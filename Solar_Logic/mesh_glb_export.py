from __future__ import annotations
from array import array
from pathlib import Path
from .binary import EndianReader
from .gltf_writer import (
    EXACT_NORMAL,
    EXACT_TANGENT,
    GL_ARRAY_BUFFER,
    GL_ELEMENT_ARRAY_BUFFER,
    GL_FLOAT,
    GL_UNSIGNED_INT,
    GL_UNSIGNED_SHORT,
    GlbBuilder,
    normalize3,
    uv_extra_names,
    vector_length,
)
from .mesh_convert import MeshData, MeshDecodeError, prepare_mesh_export_scene, read_mesh
from .model_context import FbxSceneContext, ModelContextIndex, SceneMaterial
from .serialized import SerializedAsset, SerializedObject
from .mesh_convert import build_texture_paths

def export_mesh_glb(
    asset: SerializedAsset,
    obj: SerializedObject,
    out_path: Path,
    model_context: ModelContextIndex | None = None,
) -> dict:
    with asset.path.open("rb") as handle:
        reader = EndianReader(handle, asset.endian)
        mesh = read_mesh(asset, reader, obj)
    if not mesh.vertices or not mesh.indices:
        raise MeshDecodeError("mesh has no vertices or triangle indices")

    scene = model_context.scene_for_mesh(asset, obj) if model_context is not None else None
    scene, coordinate_space = prepare_mesh_export_scene(mesh, scene)
    taildata = {
        "sunflowyrTaildata": 1,
        "kind": "mesh_glb",
        "container": asset.relative_path,
        "appendTarget": asset.relative_path,
        "pathId": obj.path_id,
        "classId": obj.class_id,
        "className": obj.type_name,
        "name": mesh.name,
    }
    builder = GlbBuilder(mesh.name or "Mesh", taildata)
    builder.texture_uri_resolver = lambda texture: build_texture_paths(Path(out_path), texture)[1]
    mesh_index = add_roundtrip_mesh(builder, mesh, scene, asset, obj)
    attach_mesh_scene(builder, mesh, mesh_index, scene)
    metrics = builder.write(out_path)
    metrics.update(
        {
            "vertices": len(mesh.vertices),
            "triangles": len(mesh.indices) // 3,
            "submeshes": len(mesh.submesh_indices),
            "skinBones": len(mesh.bind_poses),
            "blendShapeFrames": len(mesh.blend_shape_frames),
            "materialSlots": len(scene.materials) if scene is not None else len(mesh.submesh_indices),
            "coordinateSpace": coordinate_space,
            "writeback": "unity_5_4_mesh",
        }
    )
    return metrics

def add_roundtrip_mesh(
    builder: GlbBuilder,
    mesh: MeshData,
    scene: FbxSceneContext | None,
    asset: SerializedAsset,
    obj: SerializedObject,
) -> int:
    vertex_count = len(mesh.vertices)
    positions = array("f")
    for x, y, z in mesh.vertices:
        positions.extend((-x, y, z))
    position_min, position_max = vector_min_max(positions, 3)
    attributes: dict[str, int] = {
        "POSITION": builder.add_accessor(
            positions,
            GL_FLOAT,
            vertex_count,
            "VEC3",
            target=GL_ARRAY_BUFFER,
            minimum=position_min,
            maximum=position_max,
        )
    }

    if len(mesh.normals) == vertex_count:
        lengths = array("f")
        units = array("f")
        for x, y, z in mesh.normals:
            lengths.append(vector_length((x, y, z)))
            ux, uy, uz = normalize3((x, y, z))
            units.extend((-ux, uy, uz))
        attributes["NORMAL"] = builder.add_accessor(
            units, GL_FLOAT, vertex_count, "VEC3", target=GL_ARRAY_BUFFER
        )
        if any(abs(length - 1.0) > 1e-5 for length in lengths):
            attributes[EXACT_NORMAL] = builder.add_accessor(
                lengths, GL_FLOAT, vertex_count, "SCALAR", target=GL_ARRAY_BUFFER
            )
    if len(mesh.tangents) == vertex_count:
        parts = [array("f") for unused in EXACT_TANGENT]
        units = array("f")
        for x, y, z, w in mesh.tangents:
            for part, value in zip(parts, (-x, y, z, -w)):
                part.append(value)
            ux, uy, uz = normalize3((x, y, z))
            units.extend((-ux, uy, uz, -w))
        attributes["TANGENT"] = builder.add_accessor(
            units, GL_FLOAT, vertex_count, "VEC4", target=GL_ARRAY_BUFFER
        )
        for name, part in zip(EXACT_TANGENT, parts):
            attributes[name] = builder.add_accessor(
                part, GL_FLOAT, vertex_count, "SCALAR", target=GL_ARRAY_BUFFER
            )
    if len(mesh.colors) == vertex_count:
        colors = array("f", (component for color in mesh.colors for component in color))
        attributes["COLOR_0"] = builder.add_accessor(
            colors, GL_FLOAT, vertex_count, "VEC4", target=GL_ARRAY_BUFFER
        )
    for uv_index, uv_layer in enumerate(mesh.uv_layers[:4]):
        if len(uv_layer) != vertex_count:
            continue
        values = array("f", (component for uv in uv_layer for component in uv))
        attributes[f"TEXCOORD_{uv_index}"] = builder.add_accessor(
            values, GL_FLOAT, vertex_count, "VEC2", target=GL_ARRAY_BUFFER
        )
        extra = mesh.uv_extra[uv_index] if uv_index < len(mesh.uv_extra) else []
        if len(extra) == vertex_count and extra[0]:
            for component, name in enumerate(uv_extra_names(uv_index)[: len(extra[0])]):
                attributes[name] = builder.add_accessor(
                    array("f", (float(row[component]) for row in extra)), GL_FLOAT, vertex_count, "SCALAR", target=GL_ARRAY_BUFFER
                )

    if len(mesh.skin) == vertex_count:
        joints = array("H")
        weights = array("f")
        for bone_weight in mesh.skin:
            joints.extend(max(0, min(65535, int(value))) for value in bone_weight.indices)
            total = sum(max(0.0, float(value)) for value in bone_weight.weights)
            if total <= 0.0:
                weights.extend((1.0, 0.0, 0.0, 0.0))
            else:
                weights.extend(max(0.0, float(value)) / total for value in bone_weight.weights)
        attributes["JOINTS_0"] = builder.add_accessor(
            joints, GL_UNSIGNED_SHORT, vertex_count, "VEC4", target=GL_ARRAY_BUFFER
        )
        attributes["WEIGHTS_0"] = builder.add_accessor(
            weights, GL_FLOAT, vertex_count, "VEC4", target=GL_ARRAY_BUFFER
        )

    targets, target_metadata = add_blend_shape_targets(builder, mesh)
    materials = material_slots(mesh, scene)
    primitives: list[dict] = []
    for slot, source_indices in enumerate(mesh.submesh_indices):
        if not source_indices:
            continue
        indices = array("I")
        for index in range(0, len(source_indices), 3):
            triangle = source_indices[index : index + 3]
            if len(triangle) == 3:
                indices.extend((triangle[2], triangle[1], triangle[0]))
        primitive: dict = {
            "attributes": dict(attributes),
            "indices": builder.add_accessor(
                indices,
                GL_UNSIGNED_INT,
                len(indices),
                "SCALAR",
                target=GL_ELEMENT_ARRAY_BUFFER,
            ),
            "material": builder.add_material(materials[min(slot, len(materials) - 1)]),
            "mode": 4,
            "extras": {"unitySubmeshIndex": slot, "unityMaterialSlot": slot},
        }
        if targets:
            primitive["targets"] = [dict(target) for target in targets]
        primitives.append(primitive)

    mesh_extras = {
        "unitySource": {
            "container": asset.relative_path,
            "pathId": obj.path_id,
            "classId": obj.class_id,
        },
        "unityVertexCount": vertex_count,
        "unityBoneNameHashes": mesh.bone_name_hashes,
        "unityRootBoneNameHash": mesh.root_bone_name_hash,
        "unityBindPoses": [list(values) for values in mesh.bind_poses],
        "unityBlendShapes": target_metadata,
        "targetNames": [item["name"] for item in target_metadata],
        "sunflowyrWriteback": "replace_existing_mesh",
    }
    output: dict = {
        "name": mesh.name or "Mesh",
        "primitives": primitives,
        "extras": mesh_extras,
    }
    if targets:
        output["weights"] = [0.0] * len(targets)
    mesh_index = len(builder.document["meshes"])
    builder.document["meshes"].append(output)
    return mesh_index

def add_blend_shape_targets(builder: GlbBuilder, mesh: MeshData) -> tuple[list[dict], list[dict]]:
    targets: list[dict] = []
    metadata: list[dict] = []
    vertex_count = len(mesh.vertices)
    for frame_index, frame in enumerate(mesh.blend_shape_frames):
        position_values = array("f", [0.0] * (vertex_count * 3))
        normal_values = array("f", [0.0] * (vertex_count * 3))
        tangent_values = array("f", [0.0] * (vertex_count * 3))
        start = frame.first_vertex
        end = min(len(mesh.blend_shape_vertices), start + frame.vertex_count)
        for item in mesh.blend_shape_vertices[start:end]:
            if not (0 <= item.index < vertex_count):
                continue
            offset = item.index * 3
            position_values[offset : offset + 3] = array(
                "f", (-item.vertex[0], item.vertex[1], item.vertex[2])
            )
            normal_values[offset : offset + 3] = array(
                "f", (-item.normal[0], item.normal[1], item.normal[2])
            )
            tangent_values[offset : offset + 3] = array(
                "f", (-item.tangent[0], item.tangent[1], item.tangent[2])
            )
        target = {
            "POSITION": builder.add_accessor(position_values, GL_FLOAT, vertex_count, "VEC3")
        }
        if frame.has_normals:
            target["NORMAL"] = builder.add_accessor(normal_values, GL_FLOAT, vertex_count, "VEC3")
        if frame.has_tangents:
            target["TANGENT"] = builder.add_accessor(tangent_values, GL_FLOAT, vertex_count, "VEC3")
        channel = channel_for_frame(mesh, frame_index)
        frame_offset = frame_index - channel.frame_index if channel is not None else 0
        weight_index = (channel.frame_index + frame_offset) if channel is not None else frame_index
        full_weight = (
            mesh.blend_shape_weights[weight_index]
            if 0 <= weight_index < len(mesh.blend_shape_weights)
            else 100.0
        )
        channel_name = channel.name if channel is not None else f"BlendShape_{frame_index}"
        name = channel_name if channel is None or channel.frame_count <= 1 else f"{channel_name}@{full_weight:g}"
        metadata.append(
            {
                "name": name,
                "channelName": channel_name,
                "nameHash": channel.name_hash if channel is not None else 0,
                "frameIndex": frame_index,
                "channelFrameIndex": channel.frame_index if channel is not None else frame_index,
                "channelFrameCount": channel.frame_count if channel is not None else 1,
                "fullWeight": full_weight,
                "hasNormals": frame.has_normals,
                "hasTangents": frame.has_tangents,
            }
        )
        targets.append(target)
    return targets, metadata

def channel_for_frame(mesh: MeshData, frame_index: int):
    for channel in mesh.blend_shape_channels:
        if channel.frame_index <= frame_index < channel.frame_index + channel.frame_count:
            return channel
    return None

def material_slots(mesh: MeshData, scene: FbxSceneContext | None) -> list[SceneMaterial]:
    if scene is not None and scene.materials:
        return list(scene.materials)
    return [
        SceneMaterial(
            key=None,
            name=f"Material Slot {slot}",
            diffuse=(0.8, 0.8, 0.8, 1.0),
            ambient=(0.2, 0.2, 0.2, 1.0),
            emissive=(0.0, 0.0, 0.0, 1.0),
            specular=(0.2, 0.2, 0.2, 1.0),
            reflection=(0.0, 0.0, 0.0, 1.0),
            shininess=20.0,
            transparency=0.0,
            alpha_mode="OPAQUE",
            textures={},
        )
        for slot in range(max(1, len(mesh.submesh_indices)))
    ]

def attach_mesh_scene(
    builder: GlbBuilder,
    mesh: MeshData,
    mesh_index: int,
    scene: FbxSceneContext | None,
) -> None:
    node_indices: dict[tuple[str, int], int] = {}
    if scene is not None:
        for node in scene.nodes:
            node_indices[node.key] = builder.add_node(
                node.name,
                translation=(-node.local_position[0], node.local_position[1], node.local_position[2]),
                rotation=(
                    node.local_rotation[0],
                    -node.local_rotation[1],
                    -node.local_rotation[2],
                    node.local_rotation[3],
                ),
                scale=node.local_scale,
                extras={
                    "unityContainer": node.key[0],
                    "unityTransformPathId": node.key[1],
                    "unityBone": node.is_bone,
                },
            )
        for node in scene.nodes:
            children = [
                node_indices[child.key]
                for child in scene.nodes
                if child.parent_key == node.key and child.key in node_indices
            ]
            builder.set_node_children(node_indices[node.key], children)

    mesh_node = node_indices.get(scene.mesh_model_key) if scene is not None else None
    if mesh_node is None:
        mesh_node = builder.add_node(mesh.name or "Mesh")
    builder.document["nodes"][mesh_node]["mesh"] = mesh_index

    skin_index = add_skin(builder, mesh, scene, node_indices)
    if skin_index is not None:
        builder.document["nodes"][mesh_node]["skin"] = skin_index

    roots = []
    if scene is not None:
        roots = [
            node_indices[node.key]
            for node in scene.nodes
            if node.parent_key is None and node.key in node_indices
        ]
    if mesh_node not in roots and (scene is None or scene.mesh_model_key not in node_indices):
        roots.append(mesh_node)
    builder.set_scene_roots(roots or [mesh_node])

def add_skin(
    builder: GlbBuilder,
    mesh: MeshData,
    scene: FbxSceneContext | None,
    node_indices: dict[tuple[str, int], int],
) -> int | None:
    if len(mesh.skin) != len(mesh.vertices) or not mesh.bind_poses:
        return None
    joints: list[int] = []
    for bone_index in range(len(mesh.bind_poses)):
        bone_key = (
            scene.bone_keys[bone_index]
            if scene is not None and bone_index < len(scene.bone_keys)
            else None
        )
        node_index = node_indices.get(bone_key) if bone_key is not None else None
        if node_index is None:
            node_index = builder.add_node(
                f"Bone_{bone_index}",
                extras={
                    "unityBone": True,
                    "unityBoneIndex": bone_index,
                    "unityBoneNameHash": (
                        mesh.bone_name_hashes[bone_index]
                        if bone_index < len(mesh.bone_name_hashes)
                        else 0
                    ),
                },
            )
        else:
            extras = builder.document["nodes"][node_index].setdefault("extras", {})
            extras["unityBoneIndex"] = bone_index
            extras["unityBoneNameHash"] = (
                mesh.bone_name_hashes[bone_index]
                if bone_index < len(mesh.bone_name_hashes)
                else 0
            )
        joints.append(node_index)

    matrices = array("f")
    for values in mesh.bind_poses:
        matrices.extend(gltf_bind_pose(values))
    inverse_bind_accessor = builder.add_accessor(
        matrices,
        GL_FLOAT,
        len(mesh.bind_poses),
        "MAT4",
    )
    skin: dict = {
        "name": f"{mesh.name} Skin",
        "joints": joints,
        "inverseBindMatrices": inverse_bind_accessor,
        "extras": {"sunflowyrPreserveUnityBoneOrder": True},
    }
    skin_index = len(builder.document["skins"])
    builder.document["skins"].append(skin)
    return skin_index

def gltf_bind_pose(values: tuple[float, ...]) -> list[float]:
    matrix = [
        [float(values[row * 4 + column]) for column in range(4)]
        for row in range(4)
    ]
    reflection = (-1.0, 1.0, 1.0, 1.0)
    for row in range(4):
        for column in range(4):
            matrix[row][column] *= reflection[row] * reflection[column]
    return [matrix[row][column] for column in range(4) for row in range(4)]

def vector_min_max(values: array, dimensions: int) -> tuple[list[float], list[float]]:
    minimum = [float("inf")] * dimensions
    maximum = [float("-inf")] * dimensions
    for offset in range(0, len(values), dimensions):
        for dimension in range(dimensions):
            value = float(values[offset + dimension])
            minimum[dimension] = min(minimum[dimension], value)
            maximum[dimension] = max(maximum[dimension], value)
    return minimum, maximum
