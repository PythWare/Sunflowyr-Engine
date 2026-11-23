from __future__ import annotations
import json, math, struct
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from .binary import EndianReader
from .mesh_convert import (
    BlendShapeChannelInfo,
    BlendShapeFrameInfo,
    BlendShapeVertexInfo,
    BoneWeight,
    MeshData,
    SubMeshInfo,
    read_mesh,
)
from .gltf_writer import EXACT_NORMAL, EXACT_TANGENT, uv_extra_names
from .media_settings import apply_media_settings
from .mono_repack import EndianWriter
from .serialized import SerializedAsset, SerializedObject, load_serialized_asset

class MeshRepackError(ValueError):
    pass

COMPONENT_FORMATS = {
    5120: ("b", 1),
    5121: ("B", 1),
    5122: ("h", 2),
    5123: ("H", 2),
    5125: ("I", 4),
    5126: ("f", 4),
}
NORMALIZE_DIVISORS = {5120: 127.0, 5121: 255.0, 5122: 32767.0, 5123: 65535.0, 5125: 1.0}
ACCESSOR_DIMENSIONS = {
    "SCALAR": 1,
    "VEC2": 2,
    "VEC3": 3,
    "VEC4": 4,
    "MAT2": 4,
    "MAT3": 9,
    "MAT4": 16,
}

@dataclass(slots=True)
class GlbData:
    document: dict[str, Any]
    binary: bytes

@dataclass(slots=True)
class PrimitiveData:
    submesh_index: int
    position_accessor: int
    positions: list[tuple[float, ...]]
    normals: list[tuple[float, ...]]
    tangents: list[tuple[float, ...]]
    colors: list[tuple[float, ...]]
    uv_layers: list[list[tuple[float, ...]]]
    joints: list[tuple[int, ...]]
    weights: list[tuple[float, ...]]
    indices: list[int]
    targets: list[dict[str, list[tuple[float, ...]]]]
    normal_lengths: list[float]
    uv_extra: list[list[tuple[float, ...]]]

@dataclass(slots=True)
class SkinImportData:
    mapping: dict[int, int]
    bind_poses: list[tuple[float, ...]]

def repack_mesh_glb(glb_path: Path, taildata: dict[str, Any], data_root: Path) -> bytes:
    container = taildata_text(taildata, "container") or taildata_text(taildata, "appendTarget")
    path_id = taildata_int(taildata, "pathId")
    if not container or not path_id:
        raise MeshRepackError("Mesh GLB taildata doesnt identify a Unity Mesh object")
    asset_path = (Path(data_root) / container).resolve()
    asset = load_serialized_asset(asset_path, Path(data_root))
    obj = next(
        (item for item in asset.objects if item.path_id == path_id and item.class_id == 43),
        None,
    )
    if obj is None:
        raise MeshRepackError(f"Mesh path ID {path_id} wasnt found in {container}")
    with asset.path.open("rb") as handle:
        original = read_mesh(asset, EndianReader(handle, asset.endian), obj)
        handle.seek(obj.byte_start)
        original_raw = handle.read(obj.byte_size)
    imported = read_mesh_glb(Path(glb_path), original)
    return finish_mesh(imported, original, original_raw, asset.endian, Path(glb_path))

def finish_mesh(imported: MeshData, original: MeshData, original_raw: bytes, endian: str, settings_source: Path | None = None) -> bytes:
    from .media_settings import BAKED_FIELDS, CONTENT_FIELDS, blob_bytes, blob_value, schema_tree
    from .unity_typetree_codec import read_typetree, write_typetree

    produced = write_unity_mesh(imported, endian)
    tree = schema_tree(43)
    value = read_typetree(produced, tree, endian)
    base = read_typetree(original_raw, tree, endian)
    cooked_fits = geometry_unchanged(imported, original)
    for key, item in base.items():
        if key == "m_Name" or key in CONTENT_FIELDS[43] or key not in value:
            continue
        value[key] = item if key not in BAKED_FIELDS or cooked_fits else blob_value(b"")
    if not cooked_fits and any(blob_bytes(base.get(key)) for key in BAKED_FIELDS):
        value["m_IsReadable"] = True
        value["m_KeepVertices"] = True
        value["m_KeepIndices"] = True
    payload = write_typetree(value, tree, endian)
    if settings_source is not None:
        payload = apply_media_settings(payload, 43, endian, settings_source, cooked_fits)
    return payload

def geometry_unchanged(imported: MeshData, original: MeshData) -> bool:
    if len(imported.vertices) != len(original.vertices) or imported.submesh_indices != original.submesh_indices:
        return False
    return all(
        abs(left - right) <= 1e-5 * max(1.0, abs(right))
        for new_vertex, old_vertex in zip(imported.vertices, original.vertices)
        for left, right in zip(new_vertex, old_vertex)
    )

def read_mesh_glb(glb_path: Path | bytes, original: MeshData) -> MeshData:
    glb = read_glb(glb_path)
    mesh_index, mesh_document = select_mesh(glb.document)
    primitives = read_primitives(glb, mesh_document)
    if not primitives:
        raise MeshRepackError("GLB has no triangle mesh primitives")
    primitives.sort(key=lambda item: item.submesh_index)

    any_normals = any(item.normals for item in primitives)
    any_tangents = any(item.tangents for item in primitives)
    any_colors = any(item.colors for item in primitives)
    uv_count = max(
        (
            max((index + 1 for index, layer in enumerate(item.uv_layers) if layer), default=0)
            for item in primitives
        ),
        default=0,
    )
    any_skin = any(item.joints and item.weights for item in primitives)
    skin_data = read_skin_import_data(glb, mesh_index, original) if any_skin else SkinImportData({}, [])
    node_skin_map = skin_data.mapping
    target_count = max((len(item.targets) for item in primitives), default=0)
    if any_skin and original.bind_poses and not node_skin_map:
        raise MeshRepackError(
            "every skin joint needs unityBoneIndex or unityBoneNameHash data, export the GLB from Sunflowyr"
        )

    vertices: list[tuple[float, float, float]] = []
    normals: list[tuple[float, float, float]] = []
    tangents: list[tuple[float, float, float, float]] = []
    colors: list[tuple[float, float, float, float]] = []
    uv_layers: list[list[tuple[float, float]]] = [[] for uv_index in range(min(4, uv_count))]
    skin: list[BoneWeight] = []
    submesh_indices: list[list[int]] = []
    target_positions = [[] for target_index in range(target_count)]
    target_normals = [[] for target_index in range(target_count)]
    target_tangents = [[] for target_index in range(target_count)]
    target_has_normals = [False] * target_count
    target_has_tangents = [False] * target_count
    shared_vertices = len({item.position_accessor for item in primitives}) == 1
    any_normal_lengths = any(item.normal_lengths for item in primitives)
    normal_lengths: list[float] = []
    extra_widths = [max((len(item.uv_extra[uv_index][0]) for item in primitives if uv_index < len(item.uv_extra) and item.uv_extra[uv_index]), default=0) for uv_index in range(len(uv_layers))]
    uv_extra: list[list[tuple[float, ...]]] = [[] for unused in uv_layers]

    for primitive in primitives:
        append_vertices = not shared_vertices or not vertices
        vertex_offset = 0 if shared_vertices else len(vertices)
        count = len(primitive.positions)
        if append_vertices:
            for value in primitive.positions:
                vertices.append((-float(value[0]), float(value[1]), float(value[2])))
            if any_normal_lengths:
                normal_lengths.extend(primitive.normal_lengths or [1.0] * count)
            if any_normals:
                source = primitive.normals or [(0.0, 0.0, 0.0)] * count
                normals.extend((-float(value[0]), float(value[1]), float(value[2])) for value in source)
            if any_tangents:
                source = primitive.tangents or [(0.0, 0.0, 0.0, 0.0)] * count
                tangents.extend(
                    (-float(value[0]), float(value[1]), float(value[2]), -float(value[3]))
                    for value in source
                )
            if any_colors:
                source = primitive.colors or [(1.0, 1.0, 1.0, 1.0)] * count
                colors.extend(color4(value) for value in source)
            for uv_index, output_layer in enumerate(uv_layers):
                source = (
                    primitive.uv_layers[uv_index]
                    if uv_index < len(primitive.uv_layers) and primitive.uv_layers[uv_index]
                    else [(0.0, 0.0)] * count
                )
                output_layer.extend((float(value[0]), 1.0 - float(value[1])) for value in source)
                width = extra_widths[uv_index]
                if width:
                    rows = primitive.uv_extra[uv_index] if uv_index < len(primitive.uv_extra) and primitive.uv_extra[uv_index] else []
                    uv_extra[uv_index].extend(tuple(float(value) for value in row[:width]) + (0.0,) * (width - len(row)) for row in rows or [()] * count)
            if any_skin:
                source_joints = primitive.joints or [(0, 0, 0, 0)] * count
                source_weights = primitive.weights or [(1.0, 0.0, 0.0, 0.0)] * count
                for joints, weights in zip(source_joints, source_weights):
                    try:
                        mapped = tuple(node_skin_map[int(value)] for value in joints[:4])
                    except KeyError as error:
                        raise MeshRepackError(
                            f"GLB skin references joint slot {int(error.args[0])} but that slot has no Unity bone mapping"
                        ) from error
                    normalized = normalize_weights(weights)
                    skin.append(BoneWeight(normalized, pad_int4(mapped)))

        converted_indices: list[int] = []
        for index in range(0, len(primitive.indices), 3):
            triangle = primitive.indices[index : index + 3]
            if len(triangle) == 3:
                converted_indices.extend(
                    (
                        vertex_offset + int(triangle[2]),
                        vertex_offset + int(triangle[1]),
                        vertex_offset + int(triangle[0]),
                    )
                )
        submesh_indices.append(converted_indices)

        if append_vertices:
            for target_index in range(target_count):
                target = primitive.targets[target_index] if target_index < len(primitive.targets) else {}
                positions = target.get("POSITION") or [(0.0, 0.0, 0.0)] * count
                normal_values = target.get("NORMAL") or [(0.0, 0.0, 0.0)] * count
                tangent_values = target.get("TANGENT") or [(0.0, 0.0, 0.0)] * count
                target_positions[target_index].extend(
                    (-float(value[0]), float(value[1]), float(value[2])) for value in positions
                )
                target_normals[target_index].extend(
                    (-float(value[0]), float(value[1]), float(value[2])) for value in normal_values
                )
                target_tangents[target_index].extend(
                    (-float(value[0]), float(value[1]), float(value[2])) for value in tangent_values
                )
                target_has_normals[target_index] |= "NORMAL" in target
                target_has_tangents[target_index] |= "TANGENT" in target

    if len(vertices) > 65535:
        raise MeshRepackError(
            f"the mesh has {len(vertices)} vertices, RWBY meshes allow at most 65535"
        )
    if any(index > 65535 for indices in submesh_indices for index in indices):
        raise MeshRepackError("a mesh index is over the 16 bit limit of 65535")

    blend_vertices, blend_frames, blend_channels, blend_weights = build_blend_shapes(
        mesh_document,
        target_positions,
        target_normals,
        target_tangents,
        target_has_normals,
        target_has_tangents,
    )
    if not tangents and original.tangents and len(normals) == len(vertices) and uv_layers and len(uv_layers[0]) == len(vertices):
        tangents = generate_tangents(vertices, normals, uv_layers[0], submesh_indices)
    elif tangents and any(value[3] == 0 for value in tangents) and len(normals) == len(vertices) == len(tangents) and uv_layers and len(uv_layers[0]) == len(vertices):
        made = generate_tangents(vertices, normals, uv_layers[0], submesh_indices)
        tangents = [fresh if value[3] == 0 else value for value, fresh in zip(tangents, made)]
    if len(normal_lengths) == len(normals) == len(vertices):
        normals = scale_rows(normals, normal_lengths, 3)
    submeshes = build_submesh_info(vertices, submesh_indices)
    bind_poses = skin_data.bind_poses or list(original.bind_poses)
    mesh_extras = mesh_document.get("extras") if isinstance(mesh_document, dict) else None
    mesh_extras = mesh_extras if isinstance(mesh_extras, dict) else {}
    bone_hashes = import_bone_name_hashes(mesh_extras, bind_poses, original)
    root_bone_hash = import_root_bone_hash(mesh_extras, original)
    mesh_name = str(mesh_document.get("name") or "") if isinstance(mesh_document, dict) else ""
    if not mesh_name or mesh_name == "Mesh":
        mesh_name = original.name
    if not any_skin and len(original.skin) == len(vertices):
        skin = list(original.skin)
    if any_skin and bind_poses:
        max_bone = max((max(item.indices) for item in skin), default=0)
        if max_bone >= len(bind_poses):
            raise MeshRepackError(
                f"GLB skin references bone {max_bone} but the target Mesh has {len(bind_poses)} bind poses"
            )

    return MeshData(
        name=mesh_name,
        vertices=vertices,
        indices=[index for submesh in submesh_indices for index in submesh],
        normals=normals,
        uv_layers=uv_layers,
        tangents=tangents,
        colors=colors,
        submeshes=submeshes,
        submesh_indices=submesh_indices,
        bind_poses=bind_poses,
        bone_name_hashes=bone_hashes,
        skin=skin,
        blend_shape_vertices=blend_vertices,
        blend_shape_frames=blend_frames,
        blend_shape_channels=blend_channels,
        blend_shape_weights=blend_weights,
        root_bone_name_hash=root_bone_hash,
        uv_extra=uv_extra if any(uv_extra) else [],
    )

def generate_tangents(vertices: list, normals: list, uvs: list, submesh_indices: list[list[int]]) -> list[tuple[float, float, float, float]]:
    import numpy

    positions = numpy.asarray(vertices, dtype=float).reshape(-1, 3)
    normal = numpy.asarray(normals, dtype=float).reshape(-1, 3)
    texture = numpy.asarray(uvs, dtype=float).reshape(-1, 2)
    triangles = numpy.asarray([index for submesh in submesh_indices for index in submesh[: len(submesh) - len(submesh) % 3]], dtype=numpy.int64).reshape(-1, 3)
    first = numpy.zeros_like(positions)
    second = numpy.zeros_like(positions)
    if len(triangles):
        edge_one = positions[triangles[:, 1]] - positions[triangles[:, 0]]
        edge_two = positions[triangles[:, 2]] - positions[triangles[:, 0]]
        delta_one = texture[triangles[:, 1]] - texture[triangles[:, 0]]
        delta_two = texture[triangles[:, 2]] - texture[triangles[:, 0]]
        area = delta_one[:, 0] * delta_two[:, 1] - delta_two[:, 0] * delta_one[:, 1]
        scale = numpy.divide(1.0, area, out=numpy.zeros_like(area), where=numpy.abs(area) > 1e-20)[:, None]
        along_u = (edge_one * delta_two[:, 1:2] - edge_two * delta_one[:, 1:2]) * scale
        along_v = (edge_two * delta_one[:, 0:1] - edge_one * delta_two[:, 0:1]) * scale
        for corner in range(3):
            numpy.add.at(first, triangles[:, corner], along_u)
            numpy.add.at(second, triangles[:, corner], along_v)
    tangent = first - normal * numpy.sum(normal * first, axis=1, keepdims=True)
    length = numpy.linalg.norm(tangent, axis=1, keepdims=True)
    fallback = numpy.cross(normal, numpy.where(numpy.abs(normal[:, 0:1]) < 0.9, [[1.0, 0.0, 0.0]], [[0.0, 1.0, 0.0]]))
    fallback_length = numpy.linalg.norm(fallback, axis=1, keepdims=True)
    tangent = numpy.where(length > 1e-12, tangent / numpy.maximum(length, 1e-12), fallback / numpy.maximum(fallback_length, 1e-12))
    handedness = numpy.where(numpy.sum(numpy.cross(normal, tangent) * second, axis=1) < 0.0, -1.0, 1.0)
    return [(float(x), float(y), float(z), float(w)) for (x, y, z), w in zip(tangent, handedness)]

def import_bone_name_hashes(extras: dict, bind_poses: list, original: MeshData) -> list:
    values = extras.get("unityBoneNameHashes")
    if not isinstance(values, list):
        return list(original.bone_name_hashes)
    try:
        hashes = [int(item) for item in values]
    except (TypeError, ValueError) as exc:
        raise MeshRepackError("unityBoneNameHashes must be a list of integers") from exc
    if bind_poses and len(hashes) != len(bind_poses):
        raise MeshRepackError(
            f"unityBoneNameHashes has {len(hashes)} entries but the mesh has "
            f"{len(bind_poses)} bind poses. they must match"
        )
    return hashes

def import_root_bone_hash(extras: dict, original: MeshData) -> int:
    value = extras.get("unityRootBoneNameHash")
    if value is None:
        return original.root_bone_name_hash
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise MeshRepackError("unityRootBoneNameHash must be an integer") from exc

def read_glb(path: Path | bytes) -> GlbData:
    data = path if isinstance(path, (bytes, bytearray)) else Path(path).read_bytes()
    if len(data) < 20:
        raise MeshRepackError("GLB is truncated")
    magic, version, total_size = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF" or version != 2 or total_size > len(data):
        raise MeshRepackError("file isnt a valid GLB 2.0 document")
    offset = 12
    document = None
    binary = b""
    while offset + 8 <= total_size:
        chunk_size, chunk_type = struct.unpack_from("<I4s", data, offset)
        offset += 8
        chunk = data[offset : offset + chunk_size]
        offset += chunk_size
        if chunk_type == b"JSON":
            document = json.loads(chunk.rstrip(b" \0").decode("utf-8"))
        elif chunk_type == b"BIN\0":
            binary = chunk
    if not isinstance(document, dict):
        raise MeshRepackError("GLB has no JSON document")
    return GlbData(document, binary)

def select_mesh(document: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    meshes = document.get("meshes")
    if not isinstance(meshes, list):
        raise MeshRepackError("GLB has no meshes")
    for index, mesh in enumerate(meshes):
        if not isinstance(mesh, dict):
            continue
        extras = mesh.get("extras")
        if isinstance(extras, dict) and isinstance(extras.get("unitySource"), dict):
            return index, mesh
    for index, mesh in enumerate(meshes):
        if isinstance(mesh, dict) and mesh.get("primitives"):
            return index, mesh
    raise MeshRepackError("GLB has no usable mesh")

def vector_lengths(glb: GlbData, attributes: dict[str, Any], key: str, count: int, cache: dict) -> list[float]:
    values = optional_accessor(glb, attributes.get(key), count, cache=cache)
    return [float(value[0]) for value in values] if len(values) == count else []

def exact_tangents(plain: list[tuple], parts: list[list[float]], count: int) -> list[tuple]:
    if not all(len(part) == count for part in parts) or not count:
        return plain
    import numpy

    exact = numpy.asarray(parts, dtype=numpy.float64).T
    if len(plain) != count:
        return list(map(tuple, exact.tolist()))
    given = numpy.asarray(plain, dtype=numpy.float64).reshape(count, -1)[:, :4]
    length = numpy.sqrt((exact[:, :3] ** 2).sum(axis=1))
    with numpy.errstate(divide="ignore", invalid="ignore"):
        facing = (given[:, :3] * exact[:, :3]).sum(axis=1) / length
    same = (length > 0) & (exact[:, 3] != 0) & (facing >= 0.999) & (given[:, 3] * exact[:, 3] > 0)
    return list(map(tuple, numpy.where(same[:, None], exact, given).tolist()))

def scale_rows(rows: list[tuple], lengths: list[float], width: int) -> list[tuple]:
    import numpy

    table = numpy.asarray(rows, dtype=numpy.float64).reshape(len(rows), -1)[:, :width].copy()
    wanted = numpy.asarray(lengths, dtype=numpy.float64)
    size = numpy.sqrt((table[:, :3] ** 2).sum(axis=1))
    usable = (size > 0) & (wanted > 0)
    factor = numpy.ones(len(table))
    factor[usable] = wanted[usable] / size[usable]
    table[:, :3] *= factor[:, None]
    return list(map(tuple, table.tolist()))

def read_primitives(glb: GlbData, mesh_document: dict[str, Any]) -> list[PrimitiveData]:
    output: list[PrimitiveData] = []
    accessor_cache: dict[tuple[int, bool], list[tuple]] = {}
    primitives = mesh_document.get("primitives")
    if not isinstance(primitives, list):
        return output
    for order, primitive in enumerate(primitives):
        if not isinstance(primitive, dict) or int(primitive.get("mode", 4)) != 4:
            continue
        attributes = primitive.get("attributes")
        if not isinstance(attributes, dict) or "POSITION" not in attributes:
            continue
        positions = cached_accessor(glb, int(attributes["POSITION"]), accessor_cache)
        count = len(positions)
        normals = optional_accessor(glb, attributes.get("NORMAL"), count, cache=accessor_cache)
        tangents = exact_tangents(optional_accessor(glb, attributes.get("TANGENT"), count, cache=accessor_cache), [vector_lengths(glb, attributes, name, count, accessor_cache) for name in EXACT_TANGENT], count)
        normal_lengths = vector_lengths(glb, attributes, EXACT_NORMAL, count, accessor_cache)
        colors = optional_accessor(glb, attributes.get("COLOR_0"), count, cache=accessor_cache)
        uv_layers: list[list[tuple[float, ...]]] = []
        for uv_index in range(4):
            key = f"TEXCOORD_{uv_index}"
            if key in attributes:
                uv_layers.append(optional_accessor(glb, attributes[key], count, cache=accessor_cache))
            elif uv_layers:
                uv_layers.append([])
        uv_extra: list[list[tuple[float, ...]]] = []
        for uv_index in range(len(uv_layers)):
            parts = []
            for name in uv_extra_names(uv_index):
                part = vector_lengths(glb, attributes, name, count, accessor_cache)
                if not part:
                    break
                parts.append(part)
            uv_extra.append(list(zip(*parts)) if parts else [])
        joints = optional_accessor(
            glb,
            attributes.get("JOINTS_0"),
            count,
            integer=True,
            cache=accessor_cache,
        )
        weights = optional_accessor(glb, attributes.get("WEIGHTS_0"), count, cache=accessor_cache)
        if "indices" in primitive:
            index_values = cached_accessor(
                glb,
                int(primitive["indices"]),
                accessor_cache,
                integer=True,
            )
            indices = [int(value[0]) for value in index_values]
        else:
            indices = list(range(count))
        targets: list[dict[str, list[tuple[float, ...]]]] = []
        for target in primitive.get("targets") or []:
            if not isinstance(target, dict):
                continue
            target_data: dict[str, list[tuple[float, ...]]] = {}
            for key in ("POSITION", "NORMAL", "TANGENT"):
                if key in target:
                    target_data[key] = cached_accessor(glb, int(target[key]), accessor_cache)
            targets.append(target_data)
        extras = primitive.get("extras")
        submesh_index = order
        if isinstance(extras, dict):
            submesh_index = int(extras.get("unitySubmeshIndex", order))
        output.append(
            PrimitiveData(
                submesh_index,
                int(attributes["POSITION"]),
                positions,
                normals,
                tangents,
                colors,
                uv_layers,
                [tuple(int(value) for value in item) for item in joints],
                weights,
                indices,
                targets,
                normal_lengths,
                uv_extra,
            )
        )
    return output

def read_accessor(glb: GlbData, accessor_index: int, integer: bool = False) -> list[tuple]:
    accessors = glb.document.get("accessors") or []
    views = glb.document.get("bufferViews") or []
    if not (0 <= accessor_index < len(accessors)):
        raise MeshRepackError(f"GLB accessor {accessor_index} is missing")
    accessor = accessors[accessor_index]
    if not isinstance(accessor, dict) or "bufferView" not in accessor:
        raise MeshRepackError("sparse or viewless GLB accessors arent supported")
    view_index = int(accessor["bufferView"])
    if not (0 <= view_index < len(views)):
        raise MeshRepackError("GLB bufferView is missing")
    view = views[view_index]
    component_type = int(accessor.get("componentType", 0))
    format_info = COMPONENT_FORMATS.get(component_type)
    dimensions = ACCESSOR_DIMENSIONS.get(str(accessor.get("type") or ""))
    if format_info is None or dimensions is None:
        raise MeshRepackError("GLB accessor uses an unsupported component or shape")
    format_code, component_size = format_info
    count = int(accessor.get("count", 0))
    item_size = component_size * dimensions
    stride = int(view.get("byteStride", item_size))
    offset = int(view.get("byteOffset", 0)) + int(accessor.get("byteOffset", 0))
    normalized = bool(accessor.get("normalized"))
    if count <= 0:
        return []
    if offset + (count - 1) * stride + item_size > len(glb.binary):
        raise MeshRepackError("GLB accessor runs past the end of its buffer")
    import numpy

    array = numpy.ndarray((count, dimensions), dtype=numpy.dtype("<" + format_code), buffer=glb.binary, offset=offset, strides=(stride, component_size))
    if integer:
        array = array.astype(numpy.int64)
    elif normalized and component_type != 5126:
        array = array.astype(numpy.float64) / NORMALIZE_DIVISORS[component_type]
        if component_type in (5120, 5122):
            array = numpy.maximum(array, -1.0)
    else:
        array = array.astype(numpy.float64)
    return list(map(tuple, array.tolist()))

def optional_accessor(
    glb: GlbData,
    accessor_index: Any,
    expected_count: int,
    integer: bool = False,
    cache: dict[tuple[int, bool], list[tuple]] | None = None,
) -> list[tuple]:
    if accessor_index is None:
        return []
    values = (
        cached_accessor(glb, int(accessor_index), cache, integer=integer)
        if cache is not None
        else read_accessor(glb, int(accessor_index), integer=integer)
    )
    if len(values) != expected_count:
        raise MeshRepackError("GLB primitive attribute counts dont match POSITION")
    return values

def cached_accessor(
    glb: GlbData,
    accessor_index: int,
    cache: dict[tuple[int, bool], list[tuple]],
    integer: bool = False,
) -> list[tuple]:
    key = (accessor_index, integer)
    values = cache.get(key)
    if values is None:
        values = read_accessor(glb, accessor_index, integer=integer)
        cache[key] = values
    return values

def read_skin_import_data(glb: GlbData, mesh_index: int, original: MeshData) -> SkinImportData:
    document = glb.document
    nodes = document.get("nodes") or []
    skins = document.get("skins") or []
    skin_index = None
    for node in nodes:
        if isinstance(node, dict) and int(node.get("mesh", -1)) == mesh_index and "skin" in node:
            skin_index = int(node["skin"])
            break
    if skin_index is None or not (0 <= skin_index < len(skins)):
        return SkinImportData({}, [])
    skin = skins[skin_index]
    mapping: dict[int, int] = {}
    used_bones: set[int] = set()
    hash_to_index = {
        int(value): index
        for index, value in enumerate(original.bone_name_hashes)
        if int(value) not in {int(item) for item in original.bone_name_hashes[:index]}
    }
    joint_nodes = list(skin.get("joints") or [])
    for joint_index, node_index in enumerate(joint_nodes):
        bone_index = None
        if 0 <= int(node_index) < len(nodes):
            node = nodes[int(node_index)]
            extras = node.get("extras") if isinstance(node, dict) else None
            if isinstance(extras, dict) and "unityBoneIndex" in extras:
                bone_index = int(extras["unityBoneIndex"])
            elif isinstance(extras, dict) and "unityBoneNameHash" in extras:
                bone_index = hash_to_index.get(int(extras["unityBoneNameHash"]))
        if bone_index is None and 0 <= int(node_index) < len(nodes):
            name = str(nodes[int(node_index)].get("name") or "")
            if name.startswith("Bone_") and name[5:].isdigit():
                bone_index = int(name[5:])
        if bone_index is None and len(joint_nodes) == len(original.bind_poses):
            bone_index = joint_index
        if bone_index is None:
            node_name = ""
            if 0 <= int(node_index) < len(nodes) and isinstance(nodes[int(node_index)], dict):
                node_name = str(nodes[int(node_index)].get("name") or "")
            raise MeshRepackError(
                f"skin joint {joint_index} ({node_name or 'unnamed'}) cant be matched to a game bone, "
                "keep the joint count and order when editing in Blender"
            )
        if not (0 <= bone_index < len(original.bind_poses)):
            raise MeshRepackError(
                f"Skin joint {joint_index} maps to Unity bone {bone_index} but the target Mesh has {len(original.bind_poses)} bones"
            )
        if bone_index in used_bones:
            raise MeshRepackError(f"Multiple GLB skin joints map to Unity bone {bone_index}")
        used_bones.add(bone_index)
        mapping[joint_index] = bone_index

    bind_poses = read_skin_bind_poses(glb, skin, mapping, original)
    return SkinImportData(mapping, bind_poses)

def read_skin_bind_poses(
    glb: GlbData,
    skin: dict[str, Any],
    mapping: dict[int, int],
    original: MeshData,
) -> list[tuple[float, ...]]:
    if not mapping:
        return []
    accessor_index = skin.get("inverseBindMatrices")
    if accessor_index is None:
        raise MeshRepackError("Skinned GLB writeback requires inverseBindMatrices")
    matrices = read_accessor(glb, int(accessor_index))
    if len(matrices) < len(mapping):
        raise MeshRepackError("GLB inverseBindMatrices count is smaller than the skin joint count")
    bind_poses = list(original.bind_poses)
    for joint_index, bone_index in mapping.items():
        matrix = matrices[joint_index]
        if len(matrix) != 16:
            raise MeshRepackError("GLB inverseBindMatrices accessor doesnt contain MAT4 values")
        bind_poses[bone_index] = unity_bind_pose_from_gltf(matrix)
    return bind_poses

def unity_bind_pose_from_gltf(values: tuple[float, ...]) -> tuple[float, ...]:
    reflection = (-1.0, 1.0, 1.0, 1.0)
    matrix = [[0.0] * 4 for row in range(4)]
    for row in range(4):
        for column in range(4):
            matrix[row][column] = float(values[column * 4 + row]) * reflection[row] * reflection[column]
    return tuple(matrix[row][column] for row in range(4) for column in range(4))

def build_blend_shapes(
    mesh_document: dict[str, Any],
    position_targets: list[list[tuple[float, float, float]]],
    normal_targets: list[list[tuple[float, float, float]]],
    tangent_targets: list[list[tuple[float, float, float]]],
    has_normals: list[bool],
    has_tangents: list[bool],
) -> tuple[
    list[BlendShapeVertexInfo],
    list[BlendShapeFrameInfo],
    list[BlendShapeChannelInfo],
    list[float],
]:
    extras = mesh_document.get("extras")
    metadata = extras.get("unityBlendShapes") if isinstance(extras, dict) else None
    names = extras.get("targetNames") if isinstance(extras, dict) else None
    if not isinstance(metadata, list):
        metadata = []
    if not isinstance(names, list):
        names = []
    vertices: list[BlendShapeVertexInfo] = []
    frames: list[BlendShapeFrameInfo] = []
    weights: list[float] = []
    frame_metadata: list[dict[str, Any]] = []
    for target_index, positions in enumerate(position_targets):
        item_meta = metadata[target_index] if target_index < len(metadata) and isinstance(metadata[target_index], dict) else {}
        first_vertex = len(vertices)
        for vertex_index, delta in enumerate(positions):
            normal = normal_targets[target_index][vertex_index] if vertex_index < len(normal_targets[target_index]) else (0.0, 0.0, 0.0)
            tangent = tangent_targets[target_index][vertex_index] if vertex_index < len(tangent_targets[target_index]) else (0.0, 0.0, 0.0)
            if max(abs(value) for value in (*delta, *normal, *tangent)) <= 1e-12:
                continue
            vertices.append(BlendShapeVertexInfo(delta, normal, tangent, vertex_index))
        frames.append(
            BlendShapeFrameInfo(
                first_vertex,
                len(vertices) - first_vertex,
                has_normals[target_index],
                has_tangents[target_index],
            )
        )
        frame_name = str(
            item_meta.get("channelName")
            or (names[target_index] if target_index < len(names) else f"BlendShape_{target_index}")
        )
        frame_metadata.append(
            {
                "name": frame_name,
                "nameHash": int(item_meta.get("nameHash", 0)),
            }
        )
        weights.append(float(item_meta.get("fullWeight", 100.0)))

    channels: list[BlendShapeChannelInfo] = []
    index = 0
    while index < len(frame_metadata):
        name = frame_metadata[index]["name"]
        start = index
        name_hash = frame_metadata[index]["nameHash"]
        while index < len(frame_metadata) and frame_metadata[index]["name"] == name:
            index += 1
        channels.append(BlendShapeChannelInfo(name, name_hash, start, index - start))
    return vertices, frames, channels, weights

def build_submesh_info(
    vertices: list[tuple[float, float, float]],
    submesh_indices: list[list[int]],
) -> list[SubMeshInfo]:
    output: list[SubMeshInfo] = []
    first_byte = 0
    for indices in submesh_indices:
        first_vertex = min(indices) if indices else 0
        last_vertex = max(indices) if indices else first_vertex
        output.append(
            SubMeshInfo(
                first_byte,
                len(indices),
                0,
                first_vertex,
                last_vertex - first_vertex + 1 if indices else 0,
            )
        )
        first_byte += len(indices) * 2
    return output

def write_unity_mesh(mesh: MeshData, endian: str) -> bytes:
    import numpy

    writer = EndianWriter(endian)
    writer.aligned_string(mesh.name)
    writer.i32(len(mesh.submesh_indices))
    points = numpy.asarray(mesh.vertices, dtype=numpy.float64).reshape(-1, 3)
    first_byte = 0
    for indices in mesh.submesh_indices:
        writer.u32(first_byte)
        writer.u32(len(indices))
        writer.i32(0)
        first_vertex = min(indices) if indices else 0
        last_vertex = max(indices) if indices else first_vertex
        writer.u32(first_vertex)
        writer.u32(last_vertex - first_vertex + 1 if indices else 0)
        write_bounds(writer, points[numpy.asarray(indices, dtype=numpy.int64)] if indices else points[:0])
        first_byte += len(indices) * 2

    write_blend_shapes(writer, mesh)
    writer.i32(len(mesh.bind_poses))
    for matrix in mesh.bind_poses:
        if len(matrix) != 16:
            raise MeshRepackError("bind pose matrix doesnt contain 16 floats")
        for value in matrix:
            writer.f32(value)
    writer.i32(len(mesh.bone_name_hashes))
    for value in mesh.bone_name_hashes:
        writer.u32(value)
    writer.u32(mesh.root_bone_name_hash)

    writer.u8(0)
    writer.bool(True)
    writer.bool(True)
    writer.bool(True)
    writer.align()

    flat_indices = numpy.asarray([index for submesh in mesh.submesh_indices for index in submesh], dtype=numpy.int64)
    if len(flat_indices) and (flat_indices.min() < 0 or flat_indices.max() > 0xFFFF):
        raise MeshRepackError("a mesh index is over the 16 bit limit of 65535")
    writer.i32(len(flat_indices) * 2)
    writer.write(flat_indices.astype(numpy.dtype(endian + "u2")).tobytes())
    writer.align()

    write_skin(writer, mesh.skin, len(mesh.vertices))
    write_vertex_data(writer, mesh)
    write_empty_compressed_mesh(writer)
    write_bounds(writer, points)
    writer.i32(0)
    writer.i32(0)
    writer.align()
    writer.i32(0)
    writer.align()
    return writer.bytes()

def write_blend_shapes(writer: EndianWriter, mesh: MeshData) -> None:
    writer.i32(len(mesh.blend_shape_vertices))
    for item in mesh.blend_shape_vertices:
        for value in item.vertex:
            writer.f32(value)
        for value in item.normal:
            writer.f32(value)
        for value in item.tangent:
            writer.f32(value)
        writer.u32(item.index)
    writer.i32(len(mesh.blend_shape_frames))
    for frame in mesh.blend_shape_frames:
        writer.u32(frame.first_vertex)
        writer.u32(frame.vertex_count)
        writer.bool(frame.has_normals)
        writer.bool(frame.has_tangents)
        writer.align()
    writer.i32(len(mesh.blend_shape_channels))
    for channel in mesh.blend_shape_channels:
        writer.aligned_string(channel.name)
        writer.u32(channel.name_hash)
        writer.i32(channel.frame_index)
        writer.i32(channel.frame_count)
    writer.i32(len(mesh.blend_shape_weights))
    for weight in mesh.blend_shape_weights:
        writer.f32(weight)

def write_skin(writer: EndianWriter, skin: list[BoneWeight], vertex_count: int) -> None:
    if skin and len(skin) != vertex_count:
        raise MeshRepackError("skin weight count doesnt match the imported vertex count")
    writer.i32(len(skin))
    if not skin:
        return
    import numpy

    weights = numpy.asarray([normalize_weights(item.weights) for item in skin], dtype=numpy.dtype(writer.endian + "f4"))
    indices = numpy.asarray([tuple(item.indices) for item in skin], dtype=numpy.dtype(writer.endian + "i4"))
    writer.write(numpy.concatenate([weights.view(numpy.uint8).reshape(len(skin), 16), indices.view(numpy.uint8).reshape(len(skin), 16)], axis=1).tobytes())

def write_vertex_data(writer: EndianWriter, mesh: MeshData) -> None:
    vertex_count = len(mesh.vertices)
    active: list[tuple[int, int, Any]] = [(0, 3, mesh.vertices)]
    if len(mesh.normals) == vertex_count:
        active.append((1, 3, mesh.normals))
    if len(mesh.colors) == vertex_count:
        active.append((2, 4, mesh.colors))
    for uv_index, layer in enumerate(mesh.uv_layers[:4]):
        if len(layer) == vertex_count:
            extra = mesh.uv_extra[uv_index] if uv_index < len(mesh.uv_extra) else []
            if len(extra) == vertex_count and extra and extra[0]:
                active.append((3 + uv_index, 2 + len(extra[0]), [tuple(uv) + tuple(more) for uv, more in zip(layer, extra)]))
            else:
                active.append((3 + uv_index, 2, layer))
    if len(mesh.tangents) == vertex_count:
        active.append((7, 4, mesh.tangents))
    active.sort(key=lambda item: item[0])

    offsets: dict[int, int] = {}
    stride = 0
    for channel_index, dimension, values in active:
        offsets[channel_index] = stride
        stride += dimension * 4
    channel_mask = sum(1 << channel_index for channel_index, dimension, values in active)
    writer.u32(channel_mask)
    writer.u32(vertex_count)
    writer.i32(8)
    active_map = {channel_index: (dimension, values) for channel_index, dimension, values in active}
    for channel_index in range(8):
        item = active_map.get(channel_index)
        writer.u8(0)
        writer.u8(offsets.get(channel_index, 0))
        writer.u8(0)
        writer.u8(item[0] if item is not None else 0)

    import numpy

    table = numpy.zeros((vertex_count, stride // 4), dtype=numpy.dtype(writer.endian + "f4"))
    for channel_index, dimension, values in active:
        column = offsets[channel_index] // 4
        try:
            block = numpy.asarray(values, dtype=numpy.float64)
            if block.ndim != 2 or block.shape[1] < dimension:
                raise ValueError
        except ValueError:
            block = numpy.asarray([tuple(value[:dimension]) for value in values], dtype=numpy.float64)
        table[:, column : column + dimension] = block[:, :dimension]
    data = table.tobytes()
    writer.i32(len(data))
    writer.write(data)
    writer.align()

def write_empty_compressed_mesh(writer: EndianWriter) -> None:
    for float_index in range(4):
        write_empty_packed_float(writer)
    for int_index in range(3):
        write_empty_packed_int(writer)
    write_empty_packed_float(writer)
    for int_index in range(2):
        write_empty_packed_int(writer)
    writer.u32(0)

def write_empty_packed_float(writer: EndianWriter) -> None:
    writer.u32(0)
    writer.f32(0.0)
    writer.f32(0.0)
    writer.i32(0)
    writer.align()
    writer.u8(0)
    writer.align()

def write_empty_packed_int(writer: EndianWriter) -> None:
    writer.u32(0)
    writer.i32(0)
    writer.align()
    writer.u8(0)
    writer.align()

def write_bounds(writer: EndianWriter, vertices: Any) -> None:
    if not len(vertices):
        for component_index in range(6):
            writer.f32(0.0)
        return
    import numpy

    points = numpy.asarray(vertices, dtype=numpy.float64).reshape(-1, 3)
    minimum = points.min(axis=0).tolist()
    maximum = points.max(axis=0).tolist()
    center = [(minimum[axis] + maximum[axis]) * 0.5 for axis in range(3)]
    extent = [(maximum[axis] - minimum[axis]) * 0.5 for axis in range(3)]
    for value in center:
        writer.f32(value)
    for value in extent:
        writer.f32(value)

def normalize_weights(values: Any) -> tuple[float, float, float, float]:
    output = [max(0.0, float(value)) for value in list(values)[:4]]
    while len(output) < 4:
        output.append(0.0)
    total = sum(output)
    if total <= 0.0:
        return 1.0, 0.0, 0.0, 0.0
    return tuple(value / total for value in output)

def pad_int4(values: Any) -> tuple[int, int, int, int]:
    output = [int(value) for value in list(values)[:4]]
    while len(output) < 4:
        output.append(0)
    return tuple(output)

def color4(values: Any) -> tuple[float, float, float, float]:
    output = [float(value) for value in list(values)[:4]]
    while len(output) < 4:
        output.append(1.0)
    return tuple(output)

def taildata_text(record: dict[str, Any], key: str) -> str:
    value = record.get(key)
    if value is None:
        snake = camel_to_snake(key)
        value = record.get(snake)
    return str(value or "")

def taildata_int(record: dict[str, Any], key: str) -> int:
    value = record.get(key)
    if value is None:
        value = record.get(camel_to_snake(key), 0)
    return int(value or 0)

def camel_to_snake(name: str) -> str:
    output = []
    for character in name:
        if character.isupper():
            output.append("_")
            output.append(character.lower())
        else:
            output.append(character)
    return "".join(output).lstrip("_")
