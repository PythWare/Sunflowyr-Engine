from __future__ import annotations
import json, math, os, struct, tempfile
from pathlib import Path
from typing import Any
from .mesh_convert import MeshData
from .mesh_repack import MeshRepackError, read_glb, read_mesh_glb, select_mesh, write_unity_mesh

class NewMeshAssetError(ValueError):
    pass

def build_mesh_asset(
    glb_path: Path,
    fallback_name: str,
) -> tuple[bytes, str, dict[str, object]]:
    temp_path: Path | None = None
    try:
        glb = read_glb(Path(glb_path))
        mesh_index, mesh_document = select_mesh(glb.document)
        skin_index = find_skin_index(glb.document, mesh_index)
        if skin_index is None:
            source_path = Path(glb_path)
            bone_count = 0
        else:
            source_path, bone_count = stamp_bone_indices(Path(glb_path), glb, skin_index)
            temp_path = source_path if source_path != Path(glb_path) else None
        name = clean_mesh_name(mesh_document.get("name"), fallback_name)
        original = empty_mesh(name)
        if bone_count:
            original.bind_poses = [IDENTITY_MATRIX] * bone_count
            original.bone_name_hashes = [0] * bone_count
        mesh = read_mesh_glb(source_path, original)
        validate_mesh(mesh)
        payload = write_unity_mesh(mesh, "<")
    except (MeshRepackError, OSError, ValueError, IndexError, KeyError, TypeError, struct.error) as exc:
        raise NewMeshAssetError(str(exc)) from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    skinned = bool(mesh.skin) and bool(mesh.bind_poses)
    return payload, name, {
        "sourceFormat": "glb",
        "vertices": len(mesh.vertices),
        "triangles": sum(len(indices) for indices in mesh.submesh_indices) // 3,
        "submeshes": len(mesh.submesh_indices),
        "hasNormals": len(mesh.normals) == len(mesh.vertices),
        "hasTangents": len(mesh.tangents) == len(mesh.vertices),
        "hasColors": len(mesh.colors) == len(mesh.vertices),
        "uvLayers": sum(1 for layer in mesh.uv_layers if len(layer) == len(mesh.vertices)),
        "blendShapeFrames": len(mesh.blend_shape_frames),
        "skinned": skinned,
        "boneCount": len(mesh.bind_poses),
        "materialSlots": len(mesh.submesh_indices),
    }

IDENTITY_MATRIX = (
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0,
)

def find_skin_index(document: dict[str, Any], mesh_index: int) -> int | None:
    for node in document.get("nodes") or []:
        if isinstance(node, dict) and int(node.get("mesh", -1)) == mesh_index and "skin" in node:
            return int(node["skin"])
    return None

def stamp_bone_indices(glb_path: Path, glb: Any, skin_index: int) -> tuple[Path, int]:
    document = glb.document
    skins = document.get("skins") or []
    if not (0 <= skin_index < len(skins)):
        return glb_path, 0
    joints = list(skins[skin_index].get("joints") or [])
    nodes = document.get("nodes") or []
    missing = False
    bone_indices: list[int] = []
    for order, node_index in enumerate(joints):
        node = nodes[int(node_index)] if 0 <= int(node_index) < len(nodes) else None
        extras = node.get("extras") if isinstance(node, dict) else None
        if isinstance(extras, dict) and "unityBoneIndex" in extras:
            bone_indices.append(int(extras["unityBoneIndex"]))
        else:
            missing = True
            bone_indices.append(order)
    bone_count = (max(bone_indices) + 1) if bone_indices else 0
    if not missing:
        return glb_path, bone_count
    for order, node_index in enumerate(joints):
        if not (0 <= int(node_index) < len(nodes)):
            continue
        node = nodes[int(node_index)]
        if not isinstance(node, dict):
            continue
        node.setdefault("extras", {})["unityBoneIndex"] = order
    handle, temp_name = tempfile.mkstemp(prefix="sunflowyr_skin_", suffix=".glb")
    os.close(handle)
    temp_path = Path(temp_name)
    temp_path.write_bytes(rebuild_glb(document, glb.binary))
    return temp_path, bone_count

class SkeletonNode:
    __slots__ = ("name", "parent", "position", "rotation", "scale", "bone_index", "node_index")

    def __init__(
        self,
        name: str,
        parent: int | None,
        position: tuple[float, float, float],
        rotation: tuple[float, float, float, float],
        scale: tuple[float, float, float],
        bone_index: int | None,
        node_index: int,
    ) -> None:
        self.name = name
        self.parent = parent
        self.position = position
        self.rotation = rotation
        self.scale = scale
        self.bone_index = bone_index
        self.node_index = node_index

def read_glb_skeleton(glb_path: Path) -> tuple[list[SkeletonNode], list[int], int | None]:
    try:
        glb = read_glb(Path(glb_path))
    except (MeshRepackError, OSError, ValueError) as exc:
        raise NewMeshAssetError(f"couldnt read GLB skeleton: {exc}") from exc
    document = glb.document
    nodes = document.get("nodes") or []
    skins = document.get("skins") or []
    mesh_index, mesh_document = select_mesh(document)
    skin_index = find_skin_index(document, mesh_index)
    if skin_index is None or not (0 <= skin_index < len(skins)):
        raise NewMeshAssetError("GLB has no skinned mesh, so it carries no bone hierarchy")
    skin = skins[skin_index]
    joints = [int(value) for value in (skin.get("joints") or [])]
    if not joints:
        raise NewMeshAssetError("GLB skin declares no joints")

    parent_of: dict[int, int] = {}
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        for child in node.get("children") or []:
            parent_of[int(child)] = index

    included: list[int] = []
    seen: set[int] = set()

    def include(node_index: int) -> None:
        if node_index in seen:
            return
        parent = parent_of.get(node_index)
        if parent is not None:
            include(parent)
        seen.add(node_index)
        included.append(node_index)

    for joint in joints:
        include(joint)

    slot_of = {node_index: slot for slot, node_index in enumerate(included)}
    bone_of_node: dict[int, int] = {}
    for order, node_index in enumerate(joints):
        node = nodes[node_index] if 0 <= node_index < len(nodes) else {}
        extras = node.get("extras") if isinstance(node, dict) else None
        bone_index = (
            int(extras["unityBoneIndex"])
            if isinstance(extras, dict) and "unityBoneIndex" in extras
            else order
        )
        bone_of_node[node_index] = bone_index

    skeleton: list[SkeletonNode] = []
    for node_index in included:
        node = nodes[node_index] if isinstance(nodes[node_index], dict) else {}
        translation = node.get("translation") or [0.0, 0.0, 0.0]
        rotation = node.get("rotation") or [0.0, 0.0, 0.0, 1.0]
        scale = node.get("scale") or [1.0, 1.0, 1.0]
        parent = parent_of.get(node_index)
        skeleton.append(
            SkeletonNode(
                name=str(node.get("name") or f"Bone_{node_index}"),
                parent=slot_of.get(parent) if parent is not None else None,
                position=(-float(translation[0]), float(translation[1]), float(translation[2])),
                rotation=(
                    float(rotation[0]),
                    -float(rotation[1]),
                    -float(rotation[2]),
                    float(rotation[3]),
                ),
                scale=(float(scale[0]), float(scale[1]), float(scale[2])),
                bone_index=bone_of_node.get(node_index),
                node_index=node_index,
            )
        )

    bone_count = max((n.bone_index for n in skeleton if n.bone_index is not None), default=-1) + 1
    bone_slots = [-1] * bone_count
    for slot, node in enumerate(skeleton):
        if node.bone_index is not None and 0 <= node.bone_index < bone_count:
            bone_slots[node.bone_index] = slot
    missing = [index for index, slot in enumerate(bone_slots) if slot < 0]
    if missing:
        raise NewMeshAssetError(
            f"GLB skin doesnt provide a joint for Unity bone indexes {missing[:6]}"
        )

    skeleton_root = skin.get("skeleton")
    root_slot = slot_of.get(int(skeleton_root)) if skeleton_root is not None else None
    if root_slot is None:
        root_slot = next((slot for slot, node in enumerate(skeleton) if node.parent is None), None)
    return skeleton, bone_slots, root_slot

def rebuild_glb(document: dict[str, Any], binary: bytes) -> bytes:
    json_chunk = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    json_chunk += b" " * ((-len(json_chunk)) % 4)
    output = bytearray()
    body = bytearray()
    body.extend(struct.pack("<II", len(json_chunk), 0x4E4F534A))
    body.extend(json_chunk)
    if binary:
        bin_chunk = bytes(binary)
        bin_chunk += b"\0" * ((-len(bin_chunk)) % 4)
        body.extend(struct.pack("<II", len(bin_chunk), 0x004E4942))
        body.extend(bin_chunk)
    output.extend(struct.pack("<III", 0x46546C67, 2, 12 + len(body)))
    output.extend(body)
    return bytes(output)

def empty_mesh(name: str) -> MeshData:
    return MeshData(
        name=name,
        vertices=[],
        indices=[],
        normals=[],
        uv_layers=[],
        tangents=[],
        colors=[],
        submeshes=[],
        submesh_indices=[],
        bind_poses=[],
        bone_name_hashes=[],
        skin=[],
        blend_shape_vertices=[],
        blend_shape_frames=[],
        blend_shape_channels=[],
        blend_shape_weights=[],
        root_bone_name_hash=0,
    )

def validate_mesh(mesh: MeshData) -> None:
    if not mesh.vertices:
        raise NewMeshAssetError("GLB Mesh has no vertices")
    if not mesh.submesh_indices or not any(mesh.submesh_indices):
        raise NewMeshAssetError("GLB Mesh has no triangle indices")
    vertex_count = len(mesh.vertices)
    for submesh_index, indices in enumerate(mesh.submesh_indices):
        if len(indices) % 3:
            raise NewMeshAssetError(f"GLB submesh {submesh_index} index count isnt divisible by three")
        if any(index < 0 or index >= vertex_count for index in indices):
            raise NewMeshAssetError(f"GLB submesh {submesh_index} contains an out of range vertex index")
    groups = [mesh.vertices, mesh.normals, mesh.tangents, mesh.colors, *mesh.uv_layers]
    for values in groups:
        for value in values:
            if any(not math.isfinite(float(component)) for component in value):
                raise NewMeshAssetError("GLB Mesh contains a non-finite vertex attribute")

def clean_mesh_name(value: Any, fallback: str) -> str:
    name = str(value or fallback).strip()
    if not name:
        raise NewMeshAssetError("Mesh name cant be empty")
    if len(name.encode("utf-8")) > 4096:
        raise NewMeshAssetError("Mesh name is too long")
    return name
