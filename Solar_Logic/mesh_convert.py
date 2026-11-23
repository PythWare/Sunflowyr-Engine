from __future__ import annotations
import math, os, re, struct
from dataclasses import dataclass, field
from pathlib import Path
from .binary import EndianReader
from .model_context import FbxSceneContext, SceneNode, SceneTexture
from .serialized import SerializedAsset, SerializedObject

class MeshDecodeError(ValueError):
    pass

@dataclass(slots=True)
class SubMeshInfo:
    first_byte: int
    index_count: int
    topology: int
    first_vertex: int
    vertex_count: int

@dataclass(slots=True)
class ChannelInfo:
    stream: int
    offset: int
    format: int
    dimension: int

@dataclass(slots=True)
class StreamInfo:
    channel_mask: int
    offset: int
    stride: int

@dataclass(slots=True)
class BoneWeight:
    weights: tuple[float, float, float, float]
    indices: tuple[int, int, int, int]

@dataclass(slots=True)
class BlendShapeVertexInfo:
    vertex: tuple[float, float, float]
    normal: tuple[float, float, float]
    tangent: tuple[float, float, float]
    index: int

@dataclass(slots=True)
class BlendShapeFrameInfo:
    first_vertex: int
    vertex_count: int
    has_normals: bool
    has_tangents: bool

@dataclass(slots=True)
class BlendShapeChannelInfo:
    name: str
    name_hash: int
    frame_index: int
    frame_count: int

@dataclass(slots=True)
class MeshData:
    name: str
    vertices: list[tuple[float, float, float]]
    indices: list[int]
    normals: list[tuple[float, float, float]]
    uv_layers: list[list[tuple[float, float]]]
    tangents: list[tuple[float, float, float, float]]
    colors: list[tuple[float, float, float, float]]
    submeshes: list[SubMeshInfo]
    submesh_indices: list[list[int]]
    bind_poses: list[tuple[float, ...]]
    bone_name_hashes: list[int]
    skin: list[BoneWeight]
    blend_shape_vertices: list[BlendShapeVertexInfo]
    blend_shape_frames: list[BlendShapeFrameInfo]
    blend_shape_channels: list[BlendShapeChannelInfo]
    blend_shape_weights: list[float]
    root_bone_name_hash: int
    uv_extra: list[list[tuple[float, ...]]] = field(default_factory=list)

    @property
    def uv0(self) -> list[tuple[float, float]]:
        return self.uv_layers[0] if self.uv_layers else []

@dataclass(slots=True)
class PackedFloatVector:
    num_items: int
    range: float
    start: float
    data: bytes
    bit_size: int

@dataclass(slots=True)
class PackedIntVector:
    num_items: int
    data: bytes
    bit_size: int

def prepare_mesh_export_scene(
    mesh: MeshData,
    scene: FbxSceneContext | None,
) -> tuple[FbxSceneContext | None, str]:
    if scene is not None and scene.skinned and mesh.bind_poses:
        return normalized_skinned_scene(mesh, scene), "normalized_bind_pose"
    if scene is not None:
        return asset_space_scene(mesh, scene), "mesh_asset"
    return None, "mesh_asset"

def read_mesh(asset: SerializedAsset, reader: EndianReader, obj: SerializedObject) -> MeshData:
    version = unity_version_tuple(asset.unity_version)
    reader.seek(obj.byte_start)
    name = reader.aligned_string(max_length=4096)

    use_16_bit_indices = True
    if version[0] < 3 or (version[0] == 3 and version[1] < 5):
        use_16_bit_indices = reader.i32() > 0

    if version[0] == 2 and version[1] <= 5:
        read_index_buffer(reader, use_16_bit_indices)

    submesh_count = reader.i32()
    if submesh_count < 0 or submesh_count > 100000:
        raise MeshDecodeError(f"invalid submesh count {submesh_count}")
    submeshes = [read_submesh(reader, version) for _ in range(submesh_count)]

    blend_shape_vertices: list[BlendShapeVertexInfo] = []
    blend_shape_frames: list[BlendShapeFrameInfo] = []
    blend_shape_channels: list[BlendShapeChannelInfo] = []
    blend_shape_weights: list[float] = []
    if version[0] > 4 or (version[0] == 4 and version[1] >= 1):
        (
            blend_shape_vertices,
            blend_shape_frames,
            blend_shape_channels,
            blend_shape_weights,
        ) = read_blend_shape_data(reader, version)

    bind_poses: list[tuple[float, ...]] = []
    bone_name_hashes: list[int] = []
    root_bone_name_hash = 0
    if version[0] > 4 or (version[0] == 4 and version[1] >= 3):
        bind_poses = read_matrix_array(reader)
        bone_name_hashes = read_u32_array(reader)
        root_bone_name_hash = reader.u32()

    index_buffer: list[int] = []
    vertex_data = None
    if version[0] > 2 or (version[0] == 2 and version[1] >= 6):
        if version[0] >= 2019:
            skip_minmax_aabb_array(reader)
            skip_u32_array(reader)

        mesh_compression = reader.u8()
        if version[0] >= 4:
            if version[0] < 5:
                reader.u8()
            reader.bool()
            reader.bool()
            reader.bool()
        reader.align()

        if (
            (version[0] > 2017 or (version[0] == 2017 and version[1] >= 4))
            or ((version[0] == 2017 and version[1] == 3 and version[2] == 1) and "p" in asset.unity_version)
            or ((version[0] == 2017 and version[1] == 3) and mesh_compression == 0)
        ):
            use_16_bit_indices = reader.i32() == 0

        index_buffer = read_index_buffer(reader, use_16_bit_indices)

    if version[0] < 3 or (version[0] == 3 and version[1] < 5):
        raise MeshDecodeError("legacy pre-3.5 mesh layout is not implemented")

    skin: list[BoneWeight] = []
    if version[0] < 2018 or (version[0] == 2018 and version[1] < 2):
        skin = read_skin(reader)

    if version[0] == 3 or (version[0] == 4 and version[1] <= 2):
        bind_poses = read_matrix_array(reader)

    vertex_data = read_vertex_data(reader, version)
    compressed = read_compressed_mesh(reader, version)

    reader.seek(24, 1)
    reader.i32()
    if version[0] >= 5:
        skip_u8_array(reader)
        reader.align()
        skip_u8_array(reader)
        reader.align()

    channels = decode_vertex_channels(vertex_data, version)
    compressed_channels = decompress_compressed_mesh(compressed, version, int(vertex_data["vertex_count"]))
    for key, value in compressed_channels.items():
        if value:
            channels[key] = value
    if compressed.get("indices"):
        index_buffer = compressed["indices"]

    vertex_count = best_vertex_count(channels, int(vertex_data["vertex_count"]))
    compressed_skin = decompress_compressed_skin(compressed, vertex_count)
    if compressed_skin:
        skin = compressed_skin
    vertices = as_vec3(channels.get("vertices"), vertex_count)
    normals = as_vec3(channels.get("normals"), vertex_count)
    tangents = as_vec4(channels.get("tangents"), vertex_count)
    colors = as_colors(channels.get("colors"), vertex_count)
    uv_layers = [as_uv(channels.get(f"uv{i}"), vertex_count) for i in range(8)]
    uv_extra = [uv_components(channels.get(f"uv{i}"), vertex_count) for i in range(8)]
    uv_extra = uv_extra if any(uv_extra) else []
    submesh_indices = triangulate_submeshes(submeshes, index_buffer, use_16_bit_indices)
    indices = [index for submesh in submesh_indices for index in submesh]

    if not vertices and compressed.get("present"):
        raise MeshDecodeError("mesh has compressed mesh data but no vertex positions in it or in its vertex data")

    return MeshData(
        name=name,
        vertices=vertices,
        indices=indices,
        normals=normals,
        uv_layers=uv_layers,
        tangents=tangents,
        colors=colors,
        submeshes=submeshes,
        submesh_indices=submesh_indices,
        bind_poses=bind_poses,
        bone_name_hashes=bone_name_hashes,
        skin=skin,
        blend_shape_vertices=blend_shape_vertices,
        blend_shape_frames=blend_shape_frames,
        blend_shape_channels=blend_shape_channels,
        blend_shape_weights=blend_shape_weights,
        root_bone_name_hash=root_bone_name_hash,
        uv_extra=uv_extra,
    )

def build_texture_paths(out_path: Path, texture: SceneTexture) -> tuple[str, str]:
    safe_name = safe_file_name(texture.name) or "Texture2D"
    converted_root = out_path.parents[2] if len(out_path.parents) > 2 else out_path.parent
    target = converted_root / "png" / "028_Texture2D" / f"{safe_name}__path{texture.key[1]}.png"
    absolute = str(target.resolve())
    relative = os.path.relpath(target, out_path.parent).replace("\\", "/")
    return absolute, relative

def safe_file_name(value: str, max_len: int = 120) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:max_len].rstrip(" .")

def asset_space_scene(mesh: MeshData, scene: FbxSceneContext) -> FbxSceneContext:
    return FbxSceneContext(
        mesh_model_key=scene.mesh_model_key,
        nodes=[
            SceneNode(
                key=scene.mesh_model_key,
                name=mesh.name or "Mesh",
                parent_key=None,
                local_rotation=(0.0, 0.0, 0.0, 1.0),
                local_position=(0.0, 0.0, 0.0),
                local_scale=(1.0, 1.0, 1.0),
                is_bone=False,
            )
        ],
        materials=scene.materials,
        bone_keys=[],
        subset_indices=scene.subset_indices,
        renderer_key=scene.renderer_key,
        skinned=False,
    )

def normalized_skinned_scene(mesh: MeshData, scene: FbxSceneContext) -> FbxSceneContext:
    source_nodes = {node.key: node for node in scene.nodes}
    source_globals = scene_global_matrices(scene)
    mesh_basis = without_translation(source_globals.get(scene.mesh_model_key, identity_matrix()))
    valid_bones = [
        (index, key)
        for index, key in enumerate(scene.bone_keys)
        if key is not None and key in source_nodes and index < len(mesh.bind_poses)
    ]
    if not valid_bones:
        return scene

    bone_globals: dict[tuple[str, int], tuple[tuple[float, ...], ...]] = {}
    for bone_index, bone_key in valid_bones:
        if bone_key not in bone_globals:
            bone_globals[bone_key] = matrix_multiply(
                mesh_basis,
                matrix_inverse(convert_bind_pose(mesh.bind_poses[bone_index])),
            )

    root_key = (f"__sunflowyr_bind_root__:{scene.renderer_key[0]}", scene.renderer_key[1])
    root_name = resolve_root_name(scene, source_nodes)
    nodes = [
        SceneNode(
            key=root_key,
            name=root_name,
            parent_key=None,
            local_rotation=(0.0, 0.0, 0.0, 1.0),
            local_position=(0.0, 0.0, 0.0),
            local_scale=(1.0, 1.0, 1.0),
            is_bone=False,
        ),
        scene_node_from_fbx_matrix(source_nodes[scene.mesh_model_key], root_key, mesh_basis, is_bone=False),
    ]

    bone_keys = set(bone_globals)
    for bone_key in sorted(bone_globals, key=lambda key: source_depth(key, source_nodes)):
        source = source_nodes[bone_key]
        parent_key = nearest_bone_parent(source.parent_key, bone_keys, source_nodes)
        if parent_key is None:
            local_matrix = bone_globals[bone_key]
            parent_key = root_key
        else:
            local_matrix = matrix_multiply(matrix_inverse(bone_globals[parent_key]), bone_globals[bone_key])
        nodes.append(scene_node_from_fbx_matrix(source, parent_key, local_matrix))

    return FbxSceneContext(
        mesh_model_key=scene.mesh_model_key,
        nodes=nodes,
        materials=scene.materials,
        bone_keys=scene.bone_keys,
        subset_indices=scene.subset_indices,
        renderer_key=scene.renderer_key,
        skinned=True,
    )

def resolve_root_name(
    scene: FbxSceneContext,
    source_nodes: dict[tuple[str, int], SceneNode],
) -> str:
    current = source_nodes.get(scene.mesh_model_key)
    seen: set[tuple[str, int]] = set()
    while current is not None and current.key not in seen:
        seen.add(current.key)
        parent = source_nodes.get(current.parent_key)
        if parent is None:
            return current.name
        current = parent
    return "Armature"

def source_depth(
    key: tuple[str, int],
    source_nodes: dict[tuple[str, int], SceneNode],
) -> int:
    depth = 0
    seen: set[tuple[str, int]] = set()
    current = source_nodes.get(key)
    while current is not None and current.key not in seen:
        seen.add(current.key)
        current = source_nodes.get(current.parent_key)
        depth += 1
    return depth

def nearest_bone_parent(
    key: tuple[str, int] | None,
    bone_keys: set[tuple[str, int]],
    source_nodes: dict[tuple[str, int], SceneNode],
) -> tuple[str, int] | None:
    seen: set[tuple[str, int]] = set()
    current = key
    while current is not None and current not in seen:
        if current in bone_keys:
            return current
        seen.add(current)
        node = source_nodes.get(current)
        current = node.parent_key if node is not None else None
    return None

def scene_node_from_fbx_matrix(
    source: SceneNode,
    parent_key: tuple[str, int],
    matrix: tuple[tuple[float, ...], ...],
    *,
    is_bone: bool = True,
) -> SceneNode:
    position, rotation, scale = decompose_matrix(matrix)
    return SceneNode(
        key=source.key,
        name=source.name,
        parent_key=parent_key,
        local_rotation=convert_quaternion(rotation),
        local_position=(-position[0], position[1], position[2]),
        local_scale=scale,
        is_bone=is_bone,
    )

def without_translation(
    matrix: tuple[tuple[float, ...], ...],
) -> tuple[tuple[float, ...], ...]:
    return tuple(
        tuple(0.0 if column == 3 and row < 3 else matrix[row][column] for column in range(4))
        for row in range(4)
    )

def decompose_matrix(
    matrix: tuple[tuple[float, ...], ...],
) -> tuple[
    tuple[float, float, float],
    tuple[float, float, float, float],
    tuple[float, float, float],
]:
    position = (matrix[0][3], matrix[1][3], matrix[2][3])
    columns = [tuple(matrix[row][column] for row in range(3)) for column in range(3)]
    scales = [math.sqrt(sum(value * value for value in column)) for column in columns]
    if min(scales) < 1e-12:
        raise MeshDecodeError("bind pose contains a zero scale")
    rotation = [[columns[column][row] / scales[column] for column in range(3)] for row in range(3)]
    determinant = (
        rotation[0][0] * (rotation[1][1] * rotation[2][2] - rotation[1][2] * rotation[2][1])
        - rotation[0][1] * (rotation[1][0] * rotation[2][2] - rotation[1][2] * rotation[2][0])
        + rotation[0][2] * (rotation[1][0] * rotation[2][1] - rotation[1][1] * rotation[2][0])
    )
    if determinant < 0.0:
        scales[0] = -scales[0]
        for row in range(3):
            rotation[row][0] = -rotation[row][0]
    quaternion = rotation_matrix_to_quaternion(rotation)
    return position, quaternion, tuple(scales)

def rotation_matrix_to_quaternion(
    matrix: list[list[float]],
) -> tuple[float, float, float, float]:
    trace = matrix[0][0] + matrix[1][1] + matrix[2][2]
    if trace > 0.0:
        size = math.sqrt(trace + 1.0) * 2.0
        return (
            (matrix[2][1] - matrix[1][2]) / size,
            (matrix[0][2] - matrix[2][0]) / size,
            (matrix[1][0] - matrix[0][1]) / size,
            0.25 * size,
        )
    if matrix[0][0] > matrix[1][1] and matrix[0][0] > matrix[2][2]:
        size = math.sqrt(1.0 + matrix[0][0] - matrix[1][1] - matrix[2][2]) * 2.0
        return (
            0.25 * size,
            (matrix[0][1] + matrix[1][0]) / size,
            (matrix[0][2] + matrix[2][0]) / size,
            (matrix[2][1] - matrix[1][2]) / size,
        )
    if matrix[1][1] > matrix[2][2]:
        size = math.sqrt(1.0 + matrix[1][1] - matrix[0][0] - matrix[2][2]) * 2.0
        return (
            (matrix[0][1] + matrix[1][0]) / size,
            0.25 * size,
            (matrix[1][2] + matrix[2][1]) / size,
            (matrix[0][2] - matrix[2][0]) / size,
        )
    size = math.sqrt(1.0 + matrix[2][2] - matrix[0][0] - matrix[1][1]) * 2.0
    return (
        (matrix[0][2] + matrix[2][0]) / size,
        (matrix[1][2] + matrix[2][1]) / size,
        0.25 * size,
        (matrix[1][0] - matrix[0][1]) / size,
    )

def scene_global_matrices(scene: FbxSceneContext) -> dict[tuple[str, int], tuple[tuple[float, ...], ...]]:
    nodes = {node.key: node for node in scene.nodes}
    matrices: dict[tuple[str, int], tuple[tuple[float, ...], ...]] = {}

    def resolve(node: SceneNode) -> tuple[tuple[float, ...], ...]:
        cached = matrices.get(node.key)
        if cached is not None:
            return cached
        local = trs_matrix(node)
        parent = nodes.get(node.parent_key)
        result = matrix_multiply(resolve(parent), local) if parent is not None else local
        matrices[node.key] = result
        return result

    for node in scene.nodes:
        resolve(node)
    return matrices

def trs_matrix(node: SceneNode) -> tuple[tuple[float, ...], ...]:
    x, y, z, w = normalize_quaternion(convert_quaternion(node.local_rotation))
    sx, sy, sz = node.local_scale
    position = (-node.local_position[0], node.local_position[1], node.local_position[2])
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    return (
        ((1.0 - 2.0 * (yy + zz)) * sx, (2.0 * (xy - wz)) * sy, (2.0 * (xz + wy)) * sz, position[0]),
        ((2.0 * (xy + wz)) * sx, (1.0 - 2.0 * (xx + zz)) * sy, (2.0 * (yz - wx)) * sz, position[1]),
        ((2.0 * (xz - wy)) * sx, (2.0 * (yz + wx)) * sy, (1.0 - 2.0 * (xx + yy)) * sz, position[2]),
        (0.0, 0.0, 0.0, 1.0),
    )

def convert_quaternion(quaternion: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    return (quaternion[0], -quaternion[1], -quaternion[2], quaternion[3])

def normalize_quaternion(
    quaternion: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    length = math.sqrt(sum(value * value for value in quaternion))
    if length < 1e-12:
        return (0.0, 0.0, 0.0, 1.0)
    return tuple(value / length for value in quaternion)

def convert_bind_pose(values: tuple[float, ...]) -> tuple[tuple[float, ...], ...]:
    matrix = tuple(
        tuple(float(values[row * 4 + column]) for column in range(4))
        for row in range(4)
    )
    reflection = (
        (-1.0, 0.0, 0.0, 0.0),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )
    return matrix_multiply(matrix_multiply(reflection, matrix), reflection)

def identity_matrix() -> tuple[tuple[float, ...], ...]:
    return (
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )

def matrix_multiply(
    left: tuple[tuple[float, ...], ...],
    right: tuple[tuple[float, ...], ...],
) -> tuple[tuple[float, ...], ...]:
    return tuple(
        tuple(sum(left[row][item] * right[item][column] for item in range(4)) for column in range(4))
        for row in range(4)
    )

def matrix_inverse(matrix: tuple[tuple[float, ...], ...]) -> tuple[tuple[float, ...], ...]:
    work = [list(row) + list(identity_row) for row, identity_row in zip(matrix, identity_matrix())]
    for column in range(4):
        pivot = max(range(column, 4), key=lambda row: abs(work[row][column]))
        if abs(work[pivot][column]) < 1e-12:
            raise MeshDecodeError("bind pose matrix is singular")
        work[column], work[pivot] = work[pivot], work[column]
        scale = work[column][column]
        work[column] = [value / scale for value in work[column]]
        for row in range(4):
            if row == column:
                continue
            factor = work[row][column]
            work[row] = [value - factor * pivot_value for value, pivot_value in zip(work[row], work[column])]
    return tuple(tuple(row[4:]) for row in work)

def read_submesh(reader: EndianReader, version: tuple[int, int, int, int]) -> SubMeshInfo:
    first_byte = reader.u32()
    index_count = reader.u32()
    topology = reader.i32()
    if version[0] < 4:
        reader.u32()
    if version[0] > 2017 or (version[0] == 2017 and version[1] >= 3):
        reader.u32()
    first_vertex = 0
    vertex_count = 0
    if version[0] >= 3:
        first_vertex = reader.u32()
        vertex_count = reader.u32()
        reader.seek(24, 1)
    return SubMeshInfo(first_byte, index_count, topology, first_vertex, vertex_count)

def read_index_buffer(reader: EndianReader, use_16_bit: bool) -> list[int]:
    size = reader.i32()
    if size < 0:
        raise MeshDecodeError(f"invalid index buffer size {size}")
    data = reader.read(size)
    if use_16_bit:
        reader.align()
        return [item[0] for item in struct.iter_unpack(reader.endian + "H", data)]
    return [item[0] for item in struct.iter_unpack(reader.endian + "I", data)]

def read_vertex_data(reader: EndianReader, version: tuple[int, int, int, int]) -> dict:
    current_channels = reader.u32() if version[0] < 2018 else 0
    vertex_count = reader.u32()
    channel_count = reader.i32()
    if channel_count < 0 or channel_count > 64:
        raise MeshDecodeError(f"invalid vertex channel count {channel_count}")
    channels = []
    for _ in range(channel_count):
        stream = reader.u8()
        offset = reader.u8()
        fmt = reader.u8()
        dimension = reader.u8() & 0x0F
        channels.append(ChannelInfo(stream, offset, fmt, dimension))

    if version[0] < 5:
        stream_count = reader.i32() if version[0] >= 4 else 4
        streams = []
        for _ in range(stream_count):
            channel_mask = reader.u32()
            offset = reader.u32()
            if version[0] < 4:
                stride = reader.u32()
                reader.u32()
            else:
                stride = reader.u8()
                reader.u8()
                reader.u16()
            streams.append(StreamInfo(channel_mask, offset, stride))
    else:
        streams = build_streams(channels, vertex_count, version)

    data = read_u8_array(reader)
    reader.align()
    return {
        "current_channels": current_channels,
        "vertex_count": vertex_count,
        "channels": channels,
        "streams": streams,
        "data": data,
    }

def build_streams(
    channels: list[ChannelInfo],
    vertex_count: int,
    version: tuple[int, int, int, int],
) -> list[StreamInfo]:
    if not channels:
        return []
    stream_count = max(channel.stream for channel in channels) + 1
    streams: list[StreamInfo] = []
    offset = 0
    for stream_index in range(stream_count):
        channel_mask = 0
        stride = 0
        for channel_index, channel in enumerate(channels):
            if channel.stream == stream_index and channel.dimension > 0:
                channel_mask |= 1 << channel_index
                stride += channel.dimension * vertex_format_size(to_vertex_format(channel.format, version))
        streams.append(StreamInfo(channel_mask, offset, stride))
        offset += vertex_count * stride
        offset = (offset + 15) & ~15
    return streams

def decode_vertex_channels(vertex_data: dict, version: tuple[int, int, int, int]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    vertex_count = int(vertex_data["vertex_count"])
    data: bytes = vertex_data["data"]
    streams: list[StreamInfo] = vertex_data["streams"]
    channels: list[ChannelInfo] = vertex_data["channels"]
    names = {
        0: "vertices",
        1: "normals",
        2: "colors",
        3: "uv0",
        4: "uv1",
        5: "uv2" if version[0] >= 5 else "tangents",
        6: "uv3",
        7: "tangents",
    }
    for channel_index, channel in enumerate(channels):
        dimension = channel.dimension
        if version[0] < 2018 and channel_index == 2 and channel.format == 2:
            dimension = 4
        if dimension <= 0 or channel.stream >= len(streams):
            continue
        stream = streams[channel.stream]
        if not (stream.channel_mask & (1 << channel_index)):
            continue
        vertex_format = to_vertex_format(channel.format, version)
        values = channel_values(data, stream.offset + channel.offset, stream.stride, vertex_count, dimension, vertex_format)
        name = names.get(channel_index)
        if name:
            out[name] = values
    return out

def triangulate_submeshes(
    submeshes: list[SubMeshInfo],
    index_buffer: list[int],
    use_16_bit: bool,
) -> list[list[int]]:
    result: list[list[int]] = []
    for submesh in submeshes:
        indices: list[int] = []
        first_index = submesh.first_byte // 2
        if not use_16_bit:
            first_index //= 2
        end = first_index + submesh.index_count
        if end > len(index_buffer):
            raise MeshDecodeError("submesh index range is outside index buffer")
        if submesh.topology == 0:
            for i in range(0, submesh.index_count, 3):
                indices.extend(index_buffer[first_index + i : first_index + i + 3])
        elif submesh.topology == 1:
            for i in range(0, submesh.index_count - 2):
                a = index_buffer[first_index + i]
                b = index_buffer[first_index + i + 1]
                c = index_buffer[first_index + i + 2]
                if a == b or a == c or b == c:
                    continue
                if i & 1:
                    indices.extend([b, a, c])
                else:
                    indices.extend([a, b, c])
        elif submesh.topology == 2:
            for i in range(0, submesh.index_count, 4):
                a, b, c, d = index_buffer[first_index + i : first_index + i + 4]
                indices.extend([a, b, c, a, c, d])
        else:
            raise MeshDecodeError(
                f"submesh uses line or point topology {submesh.topology}, which has no triangles to convert"
            )
        result.append(indices)
    return result

def read_compressed_mesh(reader: EndianReader, version: tuple[int, int, int, int]) -> dict:
    compressed = {
        "vertices": read_packed_float(reader),
        "uv": read_packed_float(reader),
        "bind_poses": None,
        "normals": None,
        "tangents": None,
        "weights": None,
        "normal_signs": None,
        "tangent_signs": None,
        "float_colors": None,
        "bone_indices": None,
        "triangles": None,
        "colors": None,
        "uv_info": 0,
        "indices": [],
        "present": False,
    }
    if version[0] < 5:
        compressed["bind_poses"] = read_packed_float(reader)
    compressed["normals"] = read_packed_float(reader)
    compressed["tangents"] = read_packed_float(reader)
    compressed["weights"] = read_packed_int(reader)
    compressed["normal_signs"] = read_packed_int(reader)
    compressed["tangent_signs"] = read_packed_int(reader)
    if version[0] >= 5:
        compressed["float_colors"] = read_packed_float(reader)
    compressed["bone_indices"] = read_packed_int(reader)
    compressed["triangles"] = read_packed_int(reader)
    if version[0] > 3 or (version[0] == 3 and version[1] >= 5):
        if version[0] < 5:
            compressed["colors"] = read_packed_int(reader)
        else:
            compressed["uv_info"] = reader.u32()

    vectors = [value for value in compressed.values() if isinstance(value, (PackedFloatVector, PackedIntVector))]
    compressed["present"] = any(vector.num_items > 0 for vector in vectors)
    triangle_vector = compressed.get("triangles")
    if isinstance(triangle_vector, PackedIntVector) and triangle_vector.num_items > 0:
        compressed["indices"] = unpack_ints(triangle_vector)
    return compressed

def decompress_compressed_mesh(compressed: dict, version: tuple[int, int, int, int], vertex_count_hint: int) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    vertices = compressed.get("vertices")
    if isinstance(vertices, PackedFloatVector) and vertices.num_items > 0:
        out["vertices"] = unpack_floats(vertices, 3, 3 * 4)
    vertex_count = best_vertex_count(out, vertex_count_hint)

    uv = compressed.get("uv")
    if isinstance(uv, PackedFloatVector) and uv.num_items > 0 and vertex_count > 0:
        uv_info = int(compressed.get("uv_info") or 0)
        if uv_info:
            uv_src_offset = 0
            for uv_index in range(8):
                tex_coord_bits = (uv_info >> (uv_index * 4)) & 0xF
                if tex_coord_bits & 4:
                    uv_dim = 1 + (tex_coord_bits & 3)
                    out[f"uv{uv_index}"] = unpack_floats(uv, uv_dim, uv_dim * 4, uv_src_offset, vertex_count)
                    uv_src_offset += uv_dim * vertex_count
        else:
            out["uv0"] = unpack_floats(uv, 2, 2 * 4, 0, vertex_count)
            if uv.num_items >= vertex_count * 4:
                out["uv1"] = unpack_floats(uv, 2, 2 * 4, vertex_count * 2, vertex_count)

    normals = compressed.get("normals")
    normal_signs = compressed.get("normal_signs")
    if isinstance(normals, PackedFloatVector) and isinstance(normal_signs, PackedIntVector) and normals.num_items > 0:
        normal_data = unpack_floats(normals, 2, 2 * 4)
        signs = unpack_ints(normal_signs)
        out["normals"] = decode_signed_unit_vectors(normal_data, signs, 3)

    tangents = compressed.get("tangents")
    tangent_signs = compressed.get("tangent_signs")
    if isinstance(tangents, PackedFloatVector) and isinstance(tangent_signs, PackedIntVector) and tangents.num_items > 0:
        tangent_data = unpack_floats(tangents, 2, 2 * 4)
        signs = unpack_ints(tangent_signs)
        out["tangents"] = decode_signed_unit_vectors(tangent_data, signs, 4)

    if version[0] >= 5:
        float_colors = compressed.get("float_colors")
        if isinstance(float_colors, PackedFloatVector) and float_colors.num_items > 0:
            out["colors"] = unpack_floats(float_colors, 1, 4)
    else:
        colors = compressed.get("colors")
        if isinstance(colors, PackedIntVector) and colors.num_items > 0 and colors.bit_size >= 4:
            color_vector = PackedIntVector(colors.num_items * 4, colors.data, colors.bit_size // 4)
            out["colors"] = [value / 255.0 for value in unpack_ints(color_vector)]
    return out

def decompress_compressed_skin(compressed: dict, vertex_count: int) -> list[BoneWeight]:
    weights_vector = compressed.get("weights")
    indices_vector = compressed.get("bone_indices")
    if (
        not isinstance(weights_vector, PackedIntVector)
        or not isinstance(indices_vector, PackedIntVector)
        or weights_vector.num_items <= 0
        or vertex_count <= 0
    ):
        return []
    weights = unpack_ints(weights_vector)
    bone_indices = unpack_ints(indices_vector)
    skin = [BoneWeight((0.0, 0.0, 0.0, 0.0), (0, 0, 0, 0)) for _ in range(vertex_count)]
    bone_position = 0
    bone_index_position = 0
    weight_slot = 0
    weight_sum = 0
    current_weights = [0.0, 0.0, 0.0, 0.0]
    current_indices = [0, 0, 0, 0]
    for packed_weight in weights:
        if bone_position >= vertex_count or bone_index_position >= len(bone_indices):
            break
        current_weights[weight_slot] = packed_weight / 31.0
        current_indices[weight_slot] = bone_indices[bone_index_position]
        bone_index_position += 1
        weight_slot += 1
        weight_sum += packed_weight
        if weight_sum >= 31:
            skin[bone_position] = BoneWeight(tuple(current_weights), tuple(current_indices))
            bone_position += 1
            weight_slot = 0
            weight_sum = 0
            current_weights = [0.0, 0.0, 0.0, 0.0]
            current_indices = [0, 0, 0, 0]
        elif weight_slot == 3 and bone_index_position < len(bone_indices):
            current_weights[weight_slot] = (31 - weight_sum) / 31.0
            current_indices[weight_slot] = bone_indices[bone_index_position]
            bone_index_position += 1
            skin[bone_position] = BoneWeight(tuple(current_weights), tuple(current_indices))
            bone_position += 1
            weight_slot = 0
            weight_sum = 0
            current_weights = [0.0, 0.0, 0.0, 0.0]
            current_indices = [0, 0, 0, 0]
    return skin if bone_position > 0 else []

def read_packed_float(reader: EndianReader) -> PackedFloatVector:
    num_items = reader.u32()
    value_range = reader.f32()
    start = reader.f32()
    data = read_u8_array(reader)
    reader.align()
    bit_size = reader.u8()
    reader.align()
    return PackedFloatVector(num_items, value_range, start, data, bit_size)

def read_packed_int(reader: EndianReader) -> PackedIntVector:
    num_items = reader.u32()
    data = read_u8_array(reader)
    reader.align()
    bit_size = reader.u8()
    reader.align()
    return PackedIntVector(num_items, data, bit_size)

def unpack_floats(
    vector: PackedFloatVector,
    item_count_in_chunk: int,
    chunk_stride: int,
    start: int = 0,
    num_chunks: int = -1,
) -> list[float]:
    if vector.num_items <= 0 or vector.bit_size <= 0:
        return []
    if num_chunks < 0:
        num_chunks = vector.num_items // item_count_in_chunk
    bit_pos = vector.bit_size * start
    index_pos = bit_pos // 8
    bit_pos %= 8
    max_value = (1 << vector.bit_size) - 1
    scale = vector.range / max_value if max_value else 0.0
    out: list[float] = []
    for _ in range(num_chunks):
        for item in range(item_count_in_chunk):
            value, index_pos, bit_pos = read_packed_bits(vector.data, vector.bit_size, index_pos, bit_pos)
            out.append(value * scale + vector.start)
    return out

def unpack_ints(vector: PackedIntVector) -> list[int]:
    if vector.num_items <= 0 or vector.bit_size <= 0:
        return []
    index_pos = 0
    bit_pos = 0
    mask = (1 << vector.bit_size) - 1
    out: list[int] = []
    for _ in range(vector.num_items):
        value, index_pos, bit_pos = read_packed_bits(vector.data, vector.bit_size, index_pos, bit_pos)
        out.append(value & mask)
    return out

def read_packed_bits(data: bytes, bit_size: int, index_pos: int, bit_pos: int) -> tuple[int, int, int]:
    value = 0
    bits = 0
    while bits < bit_size:
        if index_pos >= len(data):
            raise MeshDecodeError("packed mesh data ended while reading bits")
        value |= (data[index_pos] >> bit_pos) << bits
        count = min(bit_size - bits, 8 - bit_pos)
        bit_pos += count
        bits += count
        if bit_pos == 8:
            index_pos += 1
            bit_pos = 0
    value &= (1 << bit_size) - 1
    return value, index_pos, bit_pos

def decode_signed_unit_vectors(values: list[float], signs: list[int], dimensions: int) -> list[float]:
    out: list[float] = []
    pair_count = len(values) // 2
    for index in range(pair_count):
        x = values[index * 2]
        y = values[index * 2 + 1]
        z_sq = 1.0 - x * x - y * y
        if z_sq >= 0.0:
            z = math.sqrt(z_sq)
        else:
            z = 0.0
            length = math.sqrt(x * x + y * y)
            if length > 0.0:
                x /= length
                y /= length
        if index < len(signs) and signs[index if dimensions == 3 else index * 2] == 0:
            z = -z
        out.extend((x, y, z))
        if dimensions == 4:
            w_sign_index = index * 2 + 1
            out.append(1.0 if w_sign_index < len(signs) and signs[w_sign_index] > 0 else -1.0)
    return out

def best_vertex_count(channels: dict[str, list[float]], fallback: int) -> int:
    vertices = channels.get("vertices")
    if vertices and len(vertices) >= 3:
        return len(vertices) // 3
    for key, dimension in (("normals", 3), ("tangents", 4), ("colors", 4), ("uv0", 2)):
        values = channels.get(key)
        if values and len(values) >= dimension:
            return len(values) // dimension
    return fallback

def read_blend_shape_data(
    reader: EndianReader,
    version: tuple[int, int, int, int],
) -> tuple[
    list[BlendShapeVertexInfo],
    list[BlendShapeFrameInfo],
    list[BlendShapeChannelInfo],
    list[float],
]:
    if version[0] > 4 or (version[0] == 4 and version[1] >= 3):
        num_verts = reader.i32()
        vertices: list[BlendShapeVertexInfo] = []
        for vertex_index in range(num_verts):
            vertices.append(
                BlendShapeVertexInfo(
                    tuple(reader.f32() for component_index in range(3)),
                    tuple(reader.f32() for component_index in range(3)),
                    tuple(reader.f32() for component_index in range(3)),
                    reader.u32(),
                )
            )
        num_shapes = reader.i32()
        frames: list[BlendShapeFrameInfo] = []
        for shape_index in range(num_shapes):
            first_vertex = reader.u32()
            vertex_count = reader.u32()
            has_normals = reader.bool()
            has_tangents = reader.bool()
            reader.align()
            frames.append(BlendShapeFrameInfo(first_vertex, vertex_count, has_normals, has_tangents))
        num_channels = reader.i32()
        channels: list[BlendShapeChannelInfo] = []
        for channel_index in range(num_channels):
            channels.append(
                BlendShapeChannelInfo(
                    reader.aligned_string(),
                    reader.u32(),
                    reader.i32(),
                    reader.i32(),
                )
            )
        weight_count = reader.i32()
        if weight_count < 0 or weight_count > 100_000_000:
            raise MeshDecodeError("invalid blend shape weight count")
        weights = [reader.f32() for _ in range(weight_count)]
        return vertices, frames, channels, weights
    raise MeshDecodeError("legacy blend shape layout isnt implemented")

def read_skin(reader: EndianReader) -> list[BoneWeight]:
    count = reader.i32()
    if count < 0 or count > 100_000_000:
        raise MeshDecodeError("invalid skin count")
    skin: list[BoneWeight] = []
    for _ in range(count):
        weights = tuple(reader.f32() for _ in range(4))
        indices = tuple(reader.i32() for _ in range(4))
        skin.append(BoneWeight(weights, indices))
    return skin

def read_matrix_array(reader: EndianReader) -> list[tuple[float, ...]]:
    count = reader.i32()
    if count < 0 or count > 1_000_000:
        raise MeshDecodeError("invalid matrix array count")
    return [tuple(reader.f32() for _ in range(16)) for _ in range(count)]

def skip_minmax_aabb_array(reader: EndianReader) -> None:
    count = reader.i32()
    if count < 0:
        raise MeshDecodeError("invalid AABB array count")
    reader.seek(count * 24, 1)

def read_u32_array(reader: EndianReader) -> list[int]:
    count = reader.i32()
    if count < 0 or count > 100_000_000:
        raise MeshDecodeError("invalid uint array count")
    return [reader.u32() for _ in range(count)]

def skip_u32_array(reader: EndianReader) -> None:
    read_u32_array(reader)

def skip_u8_array(reader: EndianReader) -> None:
    size = reader.i32()
    if size < 0:
        raise MeshDecodeError("invalid byte array size")
    reader.seek(size, 1)

def read_u8_array(reader: EndianReader) -> bytes:
    size = reader.i32()
    if size < 0:
        raise MeshDecodeError("invalid byte array size")
    return reader.read(size)

def to_vertex_format(fmt: int, version: tuple[int, int, int, int]) -> str:
    if version[0] < 2017:
        return {
            0: "Float",
            1: "Float16",
            2: "UNorm8",
            3: "UInt8",
            4: "UInt32",
        }.get(fmt, "Float")
    if version[0] < 2019:
        return {
            0: "Float",
            1: "Float16",
            2: "UNorm8",
            3: "UNorm8",
            4: "SNorm8",
            5: "UNorm16",
            6: "SNorm16",
            7: "UInt8",
            8: "SInt8",
            9: "UInt16",
            10: "SInt16",
            11: "UInt32",
            12: "SInt32",
        }.get(fmt, "Float")
    return str(fmt)

def vertex_format_size(fmt: str) -> int:
    if fmt in ("Float", "UInt32", "SInt32"):
        return 4
    if fmt in ("Float16", "UNorm16", "SNorm16", "UInt16", "SInt16"):
        return 2
    return 1

CHANNEL_TYPES = {
    "Float": ("<f4", None, False),
    "Float16": ("<f2", None, False),
    "UNorm8": ("u1", 255.0, False),
    "SNorm8": ("i1", 127.0, True),
    "UNorm16": ("<u2", 65535.0, False),
    "SNorm16": ("<i2", 32767.0, True),
    "UInt8": ("u1", None, False),
    "SInt8": ("u1", None, False),
    "UInt16": ("<u2", None, False),
    "SInt16": ("<i2", None, False),
    "UInt32": ("<u4", None, False),
    "SInt32": ("<i4", None, False),
}

def channel_values(data: bytes, start: int, stride: int, vertex_count: int, dimension: int, fmt: str) -> list[float]:
    import numpy

    if vertex_count <= 0:
        return []
    kind = CHANNEL_TYPES.get(fmt)
    size = vertex_format_size(fmt)
    if start + (vertex_count - 1) * stride + dimension * size > len(data):
        raise MeshDecodeError("vertex data ended while reading channel")
    if kind is None:
        return [0.0] * (vertex_count * dimension)
    dtype, divisor, signed = kind
    array = numpy.ndarray((vertex_count, dimension), dtype=numpy.dtype(dtype), buffer=data, offset=start, strides=(stride, size)).astype(numpy.float64)
    if divisor is not None:
        array /= divisor
        if signed:
            numpy.maximum(array, -1.0, out=array)
    return array.ravel().tolist()

def as_vec3(values: list[float] | None, vertex_count: int) -> list[tuple[float, float, float]]:
    if not values or len(values) < vertex_count * 3:
        return []
    return [(values[i], values[i + 1], values[i + 2]) for i in range(0, vertex_count * 3, 3)]

def uv_width(values: list[float] | None, vertex_count: int) -> int:
    if not values or vertex_count <= 0:
        return 0
    return min(4, len(values) // vertex_count)

def as_uv(values: list[float] | None, vertex_count: int) -> list[tuple[float, float]]:
    width = uv_width(values, vertex_count)
    if width < 2:
        return []
    return [(values[i], 1.0 - values[i + 1]) for i in range(0, vertex_count * width, width)]

def uv_components(values: list[float] | None, vertex_count: int) -> list[tuple[float, ...]]:
    width = uv_width(values, vertex_count)
    if width <= 2:
        return []
    return [tuple(values[i + 2 : i + width]) for i in range(0, vertex_count * width, width)]

def as_vec2(values: list[float] | None, vertex_count: int) -> list[tuple[float, float]]:
    if not values or len(values) < vertex_count * 2:
        return []
    return [(values[i], 1.0 - values[i + 1]) for i in range(0, vertex_count * 2, 2)]

def as_vec4(values: list[float] | None, vertex_count: int) -> list[tuple[float, float, float, float]]:
    if not values or len(values) < vertex_count * 4:
        return []
    return [(values[i], values[i + 1], values[i + 2], values[i + 3]) for i in range(0, vertex_count * 4, 4)]

def as_colors(values: list[float] | None, vertex_count: int) -> list[tuple[float, float, float, float]]:
    if not values:
        return []
    if len(values) >= vertex_count * 4:
        return [(values[i], values[i + 1], values[i + 2], values[i + 3]) for i in range(0, vertex_count * 4, 4)]
    if len(values) >= vertex_count * 3:
        return [(values[i], values[i + 1], values[i + 2], 1.0) for i in range(0, vertex_count * 3, 3)]
    return []

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
