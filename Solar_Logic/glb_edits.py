from __future__ import annotations
import hashlib, json, re, struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
import numpy
from .asset_bundle_writer import TYPE_HASHES, pack_aligned_string
from .binary import EndianReader
from .loose_format import LooseFormatError, LooseObject, container_folder
from .serialized import load_serialized_asset

TRANSFORM_OFFSET = 12
TRANSFORM_FIELDS = 10
GAME_OBJECT = 1
TRANSFORM = 4
MESH_FILTER = 33
MESH = 43
MESH_RENDERER = 23
SKINNED_MESH_RENDERER = 137
MESH_VERTEX_LIMIT = 65535
PREFAB_FORMAT_KEY = "format"
TAG_KEYS = ("unity_container", "unity_transform_path_id", "unity_game_object_path_id")

class GlbEditError(LooseFormatError):
    pass

@dataclass(slots=True)
class TransformData:
    values: tuple[float, ...]
    children: list[tuple[int, int]]
    children_offset: int
    father: tuple[int, int]
    father_offset: int

def read_transform(raw: bytes) -> TransformData:
    values = struct.unpack_from(f"<{TRANSFORM_FIELDS}f", raw, TRANSFORM_OFFSET)
    at = TRANSFORM_OFFSET + TRANSFORM_FIELDS * 4
    count = struct.unpack_from("<i", raw, at)[0]
    children = [struct.unpack_from("<iq", raw, at + 4 + index * 12) for index in range(count)]
    father_offset = at + 4 + count * 12
    return TransformData(values, children, at, struct.unpack_from("<iq", raw, father_offset), father_offset)

def with_children(raw: bytes, children: list[tuple[int, int]]) -> bytes:
    data = read_transform(raw)
    packed = struct.pack("<i", len(children)) + b"".join(struct.pack("<iq", file_id, path_id) for file_id, path_id in children)
    return raw[: data.children_offset] + packed + raw[data.father_offset:]

def with_father(raw: bytes, father: tuple[int, int]) -> bytes:
    data = read_transform(raw)
    return raw[: data.father_offset] + struct.pack("<iq", *father) + raw[data.father_offset + 12:]

def trs_matrix(values: tuple[float, ...]) -> numpy.ndarray:
    x, y, z, w = values[:4]
    rotation = numpy.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])
    matrix = numpy.identity(4)
    matrix[:3, :3] = rotation * numpy.array(values[7:10])
    matrix[:3, 3] = values[4:7]
    return matrix

def game_object_parts(raw: bytes) -> tuple[list[tuple[int, int, int]], int, int, str]:
    count = struct.unpack_from("<i", raw, 0)[0]
    components = [struct.unpack_from("<iiq", raw, 4 + index * 16) for index in range(count)]
    at = 4 + count * 16
    length = struct.unpack_from("<i", raw, at + 4)[0]
    name = raw[at + 8: at + 8 + length].decode("utf-8", "replace")
    active_offset = at + 8 + length + (-length % 4) + 2
    return components, at, active_offset, name

def with_name(raw: bytes, name: str) -> bytes:
    unused, at, unused_active, unused_name = game_object_parts(raw)
    length = struct.unpack_from("<i", raw, at + 4)[0]
    end = at + 8 + length + (-length % 4)
    return raw[: at + 4] + pack_aligned_string(name) + raw[end:]

def compiled_name(node: dict[str, Any]) -> str | None:
    name = node.get("name")
    extras = node.get("extras") if isinstance(node.get("extras"), dict) else {}
    stored = extras.get("unity_name")
    if name is None:
        return stored if isinstance(stored, str) else None
    name = str(name)
    if isinstance(stored, str) and name != stored and re.fullmatch(re.escape(stored) + r"\.\d{3,}", name):
        return stored
    return name

def node_values(node: dict[str, Any]) -> tuple[float, ...]:
    from .prefab_from_glb import convert_position, convert_rotation, convert_scale

    if "matrix" in node:
        raise GlbEditError(f'node "{node.get("name")}" stores a matrix, export it with separate location, rotation and scale')
    rotation = convert_rotation(tuple(node.get("rotation") or (0.0, 0.0, 0.0, 1.0)))
    position = convert_position(tuple(node.get("translation") or (0.0, 0.0, 0.0)))
    scale = convert_scale(tuple(node.get("scale") or (1.0, 1.0, 1.0)))
    return (rotation["x"], rotation["y"], rotation["z"], rotation["w"], position["x"], position["y"], position["z"], scale["x"], scale["y"], scale["z"])

def accessor_bytes(document: dict[str, Any], binary: bytes, index: int) -> bytes:
    accessor = document["accessors"][int(index)]
    view = document["bufferViews"][int(accessor["bufferView"])]
    size = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}[int(accessor["componentType"])] * {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}[accessor["type"]]
    stride = int(view.get("byteStride") or size)
    start = int(view.get("byteOffset", 0)) + int(accessor.get("byteOffset", 0))
    return b"".join(binary[start + row * stride: start + row * stride + size] for row in range(int(accessor["count"]))) if stride != size else bytes(binary[start: start + size * int(accessor["count"])])

def mesh_fingerprint(document: dict[str, Any], binary: bytes, mesh_index: int) -> str:
    digest = hashlib.sha1()
    for primitive in document["meshes"][mesh_index].get("primitives") or []:
        for key, accessor in sorted((primitive.get("attributes") or {}).items()):
            digest.update(key.encode("ascii") + accessor_bytes(document, binary, accessor))
        if primitive.get("indices") is not None:
            digest.update(b"indices" + accessor_bytes(document, binary, primitive["indices"]))
        for target in primitive.get("targets") or []:
            for key, accessor in sorted(target.items()):
                digest.update(b"target" + key.encode("ascii") + accessor_bytes(document, binary, accessor))
    digest.update(json.dumps(document["meshes"][mesh_index].get("weights") or []).encode("ascii"))
    return digest.hexdigest()

@dataclass(slots=True)
class EditResult:
    objects: list[LooseObject] = field(default_factory=list)
    transforms: int = 0
    game_objects: int = 0
    rebuilt: int = 0
    separated: int = 0
    batches: int = 0
    batched_props: int = 0
    split: int = 0
    added: int = 0
    hidden: int = 0

    def summary(self) -> str:
        parts = [f"{self.transforms} transforms", f"{self.game_objects} game objects", f"{self.rebuilt} meshes"]
        if self.batches:
            parts.append(f"{self.batches} static batches rebuilt from {self.batched_props} props")
        if self.separated:
            parts.append(f"{self.separated} shared meshes given their own copy")
        if self.split:
            parts.append(f"{self.split} static props split out")
        if self.added:
            parts.append(f"{self.added} nodes added")
        if self.hidden:
            parts.append(f"{self.hidden} nodes switched off")
        return ", ".join(parts)

class GameObjects:
    def __init__(self, data_root: Path) -> None:
        self.data_root = Path(data_root)
        self.assets: dict[str, tuple[Any, dict[int, Any]]] = {}
        self.handles: dict[str, Any] = {}

    def asset(self, container: str) -> tuple[Any, dict[int, Any]]:
        if container not in self.assets:
            asset = load_serialized_asset(self.data_root / container_folder(container), self.data_root)
            self.assets[container] = (asset, {obj.path_id: obj for obj in asset.objects})
        return self.assets[container]

    def get(self, container: str, path_id: int) -> tuple[Any, bytes] | None:
        asset, by_id = self.asset(container)
        obj = by_id.get(int(path_id))
        if obj is None:
            return None
        if container not in self.handles:
            self.handles[container] = asset.path.open("rb")
        handle = self.handles[container]
        handle.seek(obj.byte_start)
        return obj, handle.read(obj.byte_size)

    def world(self, container: str, transform_id: int, cache: dict[tuple[str, int], numpy.ndarray]) -> numpy.ndarray:
        key = (container, int(transform_id))
        if key not in cache:
            found = self.get(container, transform_id)
            if found is None:
                cache[key] = numpy.identity(4)
            else:
                data = read_transform(found[1])
                local = trs_matrix(data.values)
                cache[key] = self.world(container, data.father[1], cache) @ local if data.father[0] == 0 and data.father[1] else local
        return cache[key]

    def close(self) -> None:
        for handle in self.handles.values():
            handle.close()
        self.handles.clear()

def node_tag(node: dict[str, Any]) -> dict[str, Any] | None:
    extras = node.get("extras")
    if isinstance(extras, dict) and extras.get("unity_transform_path_id") is not None and extras.get("unity_container"):
        return extras
    return None

def reachable_nodes(document: dict[str, Any]) -> set[int]:
    nodes = document.get("nodes") or []
    scenes = document.get("scenes") or []
    if not scenes:
        return set(range(len(nodes)))
    scene = scenes[int(document.get("scene") or 0)] if int(document.get("scene") or 0) < len(scenes) else scenes[0]
    found: set[int] = set()
    stack = [int(index) for index in scene.get("nodes") or []]
    while stack:
        index = stack.pop()
        if index in found or not 0 <= index < len(nodes):
            continue
        found.add(index)
        stack.extend(int(child) for child in nodes[index].get("children") or [])
    return found

def release_duplicates(nodes: list[dict[str, Any]], games: GameObjects, reachable: set[int]) -> None:
    claims: dict[tuple[str, int], list[int]] = {}
    for index, node in enumerate(nodes):
        if index not in reachable:
            node["extras"] = {key: value for key, value in (node.get("extras") or {}).items() if key not in TAG_KEYS} if isinstance(node.get("extras"), dict) else {}
            node.pop("mesh", None)
            node["children"] = []
            continue
        tag = node_tag(node)
        if tag is not None:
            claims.setdefault((str(tag["unity_container"]).lower(), int(tag["unity_transform_path_id"])), []).append(index)
    for indices in claims.values():
        if len(indices) < 2:
            continue
        tag = node_tag(nodes[indices[0]])
        found = games.get(str(tag["unity_container"]), tag.get("unity_game_object_path_id") or 0)
        game_name = game_object_parts(found[1])[3] if found is not None and found[0].class_id == GAME_OBJECT else None
        keeper = next((index for index in indices if nodes[index].get("name") == game_name), None)
        keeper = keeper if keeper is not None else next((index for index in indices if compiled_name(nodes[index]) == game_name), indices[0])
        for index in indices:
            if index != keeper:
                nodes[index]["extras"] = {key: value for key, value in nodes[index]["extras"].items() if key not in TAG_KEYS}

def is_skin_helper(nodes: list[dict[str, Any]], parents: dict[int, int | None], index: int, document: dict[str, Any]) -> bool:
    node = nodes[index]
    if node_tag(node) is not None or node.get("mesh") is None or node.get("children"):
        return False
    parent = parents.get(index)
    if parent is None or node_tag(nodes[parent]) is None:
        return False
    meshes = document.get("meshes") or []
    mesh = meshes[int(node["mesh"])] if 0 <= int(node["mesh"]) < len(meshes) else {}
    extras = mesh.get("extras") or {}
    return bool(extras.get("skinned")) and extras.get("unity_mesh_path_id") is not None

def is_mesh_helper(nodes: list[dict[str, Any]], parents: dict[int, int | None], index: int, document: dict[str, Any], games: GameObjects) -> bool:
    if is_skin_helper(nodes, parents, index, document):
        return True
    node = nodes[index]
    if node_tag(node) is not None or node.get("mesh") is None or node.get("children"):
        return False
    parent = parents.get(index)
    tag = node_tag(nodes[parent]) if parent is not None else None
    if tag is None or nodes[parent].get("mesh") is not None or not tag.get("unity_game_object_path_id"):
        return False
    meshes = document.get("meshes") or []
    extras = (meshes[int(node["mesh"])].get("extras") or {}) if 0 <= int(node["mesh"]) < len(meshes) else {}
    if extras.get("unity_renderer_path_id") is None or str(extras.get("unity_renderer_container") or "").lower() != str(tag["unity_container"]).lower():
        return False
    found = games.get(str(tag["unity_container"]), tag["unity_game_object_path_id"])
    if found is None or found[0].class_id != GAME_OBJECT:
        return False
    return any(file_id == 0 and path_id == int(extras["unity_renderer_path_id"]) for unused, file_id, path_id in game_object_parts(found[1])[0])

def decode_renderer(raw: bytes) -> dict[str, Any]:
    from .unity_typetree_codec import load_schema, read_typetree

    return read_typetree(raw, load_schema("mesh_renderer_schema.json"), "<")

def encode_renderer(document: dict[str, Any]) -> bytes:
    from .unity_typetree_codec import load_schema, write_typetree

    return write_typetree(document, load_schema("mesh_renderer_schema.json"), "<")

def edit_hierarchy(path: Path, data_root: Path, scratch: Path, source: str, allocate: Callable[[str, int, str], int], build_added: Callable[[list[Path], str], tuple[list[LooseObject], dict[str, tuple]]], scene: bool) -> EditResult:
    games = GameObjects(data_root)
    try:
        return HierarchyCompile(games, path, data_root, scratch, source, allocate, build_added, scene).run()
    finally:
        games.close()

class HierarchyCompile:
    def __init__(self, games: GameObjects, path: Path, data_root: Path, scratch: Path, source: str, allocate: Callable[[str, int, str], int], build_added: Callable[[list[Path], str], tuple[list[LooseObject], dict[str, tuple]]], scene: bool) -> None:
        from .animation_glb_import import read_glb
        from .prefab_from_glb import skinned_owners

        self.games = games
        self.data_root = Path(data_root)
        self.scratch = Path(scratch)
        self.source = source
        self.allocate = allocate
        self.build_added = build_added
        self.scene = scene
        self.document, self.binary = read_glb(Path(path).read_bytes())
        self.nodes = self.document.get("nodes") or []
        self.meshes = self.document.get("meshes") or []
        if not any(node_tag(node) for node in self.nodes):
            raise GlbEditError("this GLB lost its Sunflowyr node data, export it from Blender with Include > Custom Properties on")
        self.result = EditResult()
        self.pending: dict[tuple[str, int], tuple[int, bytes, str, str]] = {}
        self.reachable = reachable_nodes(self.document)
        release_duplicates(self.nodes, games, self.reachable)
        self.skin_owners = skinned_owners(self.document)
        self.parents: dict[int, int] = {}
        for index, node in enumerate(self.nodes):
            for child in node.get("children") or []:
                self.parents[int(child)] = index
        self.transform_nodes = {(str(node_tag(node)["unity_container"]).lower(), int(node_tag(node)["unity_transform_path_id"])): index for index, node in enumerate(self.nodes) if node_tag(node)}
        self.node_worlds: dict[int, numpy.ndarray] = {}
        self.game_worlds: dict[tuple[str, int], numpy.ndarray] = {}
        self.originals: dict[tuple[str, int], tuple[Any, bytes, str]] = {}
        self.bake: dict[int, numpy.ndarray] = {}

    def stage(self, container: str, obj: Any, payload: bytes, name: str) -> None:
        self.pending[(container.lower(), obj.path_id)] = (obj.class_id, payload, name, container)

    def current(self, container: str, path_id: int) -> tuple[Any, bytes]:
        found = self.games.get(container, path_id)
        if found is None:
            raise GlbEditError(f"{container} has no object {path_id}")
        obj, raw = found
        staged = self.pending.get((container.lower(), int(path_id)))
        return obj, (staged[1] if staged else raw)

    def run(self) -> EditResult:
        self.scratch.mkdir(parents=True, exist_ok=True)
        present = self.compile_nodes()
        self.compile_meshes()
        add_new_nodes(self.document, self.binary, self.nodes, {index: parent for index, parent in self.parents.items() if index in self.reachable}, self.reachable, self.scratch, self.games, self.current, self.stage, self.build_added, self.result, self.scene)
        hide_missing(self.nodes, present, self.games, self.current, self.stage, self.result, self.scene)
        for (unused, path_id), (class_id, payload, name, container) in self.pending.items():
            self.result.objects.append(LooseObject(container, path_id, payload, class_id, name, source=self.source))
        return self.result

    def compile_nodes(self) -> dict[str, set[int]]:
        present: dict[str, set[int]] = {}
        for node in self.nodes:
            tag = node_tag(node)
            if tag is None:
                continue
            container = str(tag["unity_container"])
            present.setdefault(container.lower(), set()).add(int(tag["unity_transform_path_id"]))
            obj, raw = self.current(container, tag["unity_transform_path_id"])
            data = bytearray(raw)
            if "skin" not in node:
                struct.pack_into(f"<{TRANSFORM_FIELDS}f", data, TRANSFORM_OFFSET, *node_values(node))
            self.stage(container, obj, bytes(data), str(node.get("name") or ""))
            self.result.transforms += 1
            owner = tag.get("unity_game_object_path_id")
            found = self.games.get(container, owner) if owner else None
            if found is not None and found[0].class_id == GAME_OBJECT:
                raw = self.current(container, owner)[1]
                name = compiled_name(node)
                self.stage(container, found[0], with_name(raw, name) if name is not None else raw, name or "")
                self.result.game_objects += 1
        return present

    def node_world(self, index: int) -> numpy.ndarray:
        if index not in self.node_worlds:
            local = trs_matrix(node_values(self.nodes[index]))
            parent = self.parents.get(index)
            if parent is not None:
                base = self.node_world(parent)
            else:
                base = numpy.identity(4)
                tag = node_tag(self.nodes[index])
                found = self.games.get(str(tag["unity_container"]), tag["unity_transform_path_id"]) if tag else None
                if found is not None:
                    father = read_transform(found[1]).father
                    if father[0] == 0 and father[1]:
                        base = self.transform_world(str(tag["unity_container"]), father[1])
            self.node_worlds[index] = base @ local
        return self.node_worlds[index]

    def transform_world(self, container: str, transform_id: int) -> numpy.ndarray:
        index = self.transform_nodes.get((container.lower(), int(transform_id)))
        return self.node_world(index) if index is not None else self.games.world(container, transform_id, self.game_worlds)

    def original(self, container: str, path_id: int) -> tuple[Any, bytes, str]:
        from .mesh_convert import read_mesh

        key = (container.lower(), int(path_id))
        if key not in self.originals:
            found = self.games.get(container, path_id)
            if found is None:
                raise GlbEditError(f"{container} has no mesh {path_id}")
            obj, raw = found
            asset = self.games.asset(container)[0]
            with asset.path.open("rb") as handle:
                mesh = read_mesh(asset, EndianReader(handle, asset.endian), obj)
            self.originals[key] = (mesh, raw, asset.endian)
        return self.originals[key]

    def imported(self, mesh_index: int, original: Any) -> Any:
        from .mesh_repack import read_mesh_glb
        from .prefab_from_glb import extract_mesh_glb

        return read_mesh_glb(extract_mesh_glb(self.document, self.binary, mesh_index, self.bone_indices(mesh_index), self.skin_owners), original)

    def bone_indices(self, mesh_index: int) -> dict[int, int] | None:
        from .prefab_from_glb import joint_bone_indices
        from .unity_typetree_codec import load_schema, read_typetree

        extras = self.meshes[mesh_index].get("extras") or {}
        if mesh_index not in self.skin_owners or extras.get("unity_renderer_path_id") is None:
            return None
        container = str(extras.get("unity_renderer_container") or extras.get("unity_container"))
        found = self.games.get(container, extras["unity_renderer_path_id"])
        if found is None or found[0].class_id != SKINNED_MESH_RENDERER:
            return None
        bones = read_typetree(found[1], load_schema("skinned_mesh_renderer_schema.json"), "<").get("m_Bones") or []
        keys: list[tuple[str, int] | None] = []
        for bone in bones:
            file_id, path_id = int(bone.get("m_FileID") or 0), int(bone.get("m_PathID") or 0)
            if file_id != 0 and path_id:
                raise GlbEditError(f"the skinned mesh {self.meshes[mesh_index].get('name')} uses bones from another game file")
            keys.append((container, path_id) if path_id else None)
        return joint_bone_indices(self.document, keys)

    def mesh_payload(self, mesh_index: int, container: str, path_id: int) -> bytes:
        from .mesh_repack import finish_mesh

        original, raw, endian = self.original(container, path_id)
        imported = self.imported(mesh_index, original)
        matrix = self.bake.get(mesh_index)
        if matrix is not None:
            linear = matrix[:3, :3]
            points = numpy.asarray(imported.vertices, dtype=float).reshape(-1, 3)
            imported.vertices = [tuple(float(value) for value in row) for row in (matrix @ numpy.c_[points, numpy.ones(len(points))].T).T[:, :3]]
            if imported.normals:
                source = numpy.asarray(imported.normals, dtype=float).reshape(-1, 3)
                turned = (numpy.linalg.inv(linear).T @ source.T).T
                imported.normals = [tuple(float(value) for value in row) for row in keep_lengths(turned, source)]
            if imported.tangents:
                source = numpy.asarray(imported.tangents, dtype=float).reshape(-1, 4)
                turned = (linear @ source[:, :3].T).T
                imported.tangents = [(*(float(value) for value in row), float(sign)) for row, sign in zip(keep_lengths(turned, source[:, :3]), source[:, 3])]
        return finish_mesh(imported, original, raw, endian)

    def owner_components(self, node_index: int) -> tuple[str, list[tuple[int, int, int]]]:
        while node_tag(self.nodes[node_index]) is None and node_index in self.parents:
            node_index = self.parents[node_index]
        tag = node_tag(self.nodes[node_index])
        if tag is None or not tag.get("unity_game_object_path_id"):
            raise GlbEditError(f'node "{self.nodes[node_index].get("name")}" has no GameObject')
        container = str(tag["unity_container"])
        return container, game_object_parts(self.current(container, tag["unity_game_object_path_id"])[1])[0]

    def mesh_target(self, mesh_index: int, owners: list[int]) -> tuple[str, int] | None:
        extras = self.meshes[mesh_index].get("extras") or {}
        if extras.get("unity_container") and extras.get("unity_mesh_path_id") is not None:
            return str(extras["unity_container"]), int(extras["unity_mesh_path_id"])
        owner = next((index for index in owners if node_tag(self.nodes[index])), None)
        if owner is None:
            return None
        container, components = self.owner_components(owner)
        entry = next((component for component in components if component[0] == MESH_FILTER and component[1] == 0), None)
        if entry is None:
            return None
        pointer = struct.unpack_from("<iq", self.current(container, entry[2])[1], 12)
        if pointer == (0, 0):
            return None
        if pointer[0] != 0:
            raise GlbEditError(f'node "{self.nodes[owner].get("name")}" uses a mesh from another file and its GLB mesh lost its Sunflowyr data')
        return container, pointer[1]

    def compile_meshes(self) -> None:
        owners: dict[int, list[int]] = {}
        for index, node in enumerate(self.nodes):
            if index in self.reachable and node.get("mesh") is not None and (node_tag(node) or is_mesh_helper(self.nodes, self.parents, index, self.document, self.games)):
                owners.setdefault(int(node["mesh"]), []).append(index)
                if node_tag(node) is None and "skin" not in node and not (self.meshes[int(node["mesh"])].get("extras") or {}).get("skinned"):
                    self.bake[int(node["mesh"])] = trs_matrix(node_values(node))
        plain: dict[tuple[str, int], list[int]] = {}
        batches: dict[tuple[str, int], list[tuple[int, int]]] = {}
        for mesh_index in sorted(owners):
            target = self.mesh_target(mesh_index, owners[mesh_index])
            if target is None:
                names = ", ".join(str(self.nodes[index].get("name")) for index in owners[mesh_index])
                raise GlbEditError(f"{names} has no game mesh to replace, add the mesh as a new node instead")
            if self.games.get(*target) is None:
                raise GlbEditError(f"{target[0]} has no mesh {target[1]}")
            key = (target[0], target[1])
            if (self.meshes[mesh_index].get("extras") or {}).get("static_batch"):
                for owner in owners[mesh_index]:
                    batches.setdefault(key, []).append((mesh_index, owner))
            else:
                plain.setdefault(key, []).append(mesh_index)
        for (container, path_id), indices in plain.items():
            obj = self.current(container, path_id)[0]
            self.stage(container, obj, self.mesh_payload(indices[0], container, path_id), str(self.meshes[indices[0]].get("name") or ""))
            self.result.rebuilt += 1
            first = mesh_fingerprint(self.document, self.binary, indices[0])
            for other in indices[1:]:
                if mesh_fingerprint(self.document, self.binary, other) == first:
                    continue
                self.separate(other, owners[other], container, path_id)
        for key, props in batches.items():
            self.compile_batch(key, props)

    def separate(self, mesh_index: int, owners: list[int], container: str, path_id: int) -> None:
        from .unity_typetree_codec import load_schema, read_typetree, write_typetree

        extras = self.meshes[mesh_index].get("extras") or {}
        name = str(self.meshes[mesh_index].get("name") or "Mesh")
        if extras.get("skinned"):
            renderer_container = str(extras.get("unity_renderer_container") or container)
            renderer_obj, renderer_raw = self.current(renderer_container, extras["unity_renderer_path_id"])
            schema = load_schema("skinned_mesh_renderer_schema.json")
            new_id = self.allocate(f"{self.source}#mesh{renderer_obj.path_id}", 1, renderer_container)
            self.add_mesh(renderer_container, new_id, self.mesh_payload(mesh_index, container, path_id), name)
            renderer = read_typetree(renderer_raw, schema, "<")
            renderer["m_Mesh"] = {"m_FileID": 0, "m_PathID": new_id}
            self.stage(renderer_container, renderer_obj, write_typetree(renderer, schema, "<"), name)
            self.result.separated += 1
            return
        for owner in owners:
            owner_container, components = self.owner_components(owner)
            entry = next((component for component in components if component[0] == MESH_FILTER and component[1] == 0), None)
            if entry is None:
                raise GlbEditError(f'node "{self.nodes[owner].get("name")}" has no MeshFilter to point at its own copy of the mesh')
            new_id = self.allocate(f"{self.source}#mesh{entry[2]}", 1, owner_container)
            self.add_mesh(owner_container, new_id, self.mesh_payload(mesh_index, container, path_id), name)
            filter_obj, filter_raw = self.current(owner_container, entry[2])
            data = bytearray(filter_raw)
            struct.pack_into("<iq", data, 12, 0, new_id)
            self.stage(owner_container, filter_obj, bytes(data), name)
            self.result.separated += 1

    def add_mesh(self, container: str, path_id: int, payload: bytes, name: str) -> None:
        self.result.objects.append(LooseObject(container, path_id, payload, MESH, name, add=("class", MESH, 0, TYPE_HASHES[MESH]), source=self.source))

    def prop_renderer(self, mesh_index: int, owner: int) -> tuple[str, Any, dict[str, Any]]:
        container, components = self.owner_components(owner)
        entry = next((component for component in components if component[0] == MESH_RENDERER and component[1] == 0), None)
        if entry is None:
            extras = self.meshes[mesh_index].get("extras") or {}
            container = str(extras.get("unity_renderer_container") or container)
            entry = (MESH_RENDERER, 0, int(extras["unity_renderer_path_id"]))
        obj, raw = self.current(container, entry[2])
        return container, obj, decode_renderer(raw)

    def compile_batch(self, key: tuple[str, int], props: list[tuple[int, int]]) -> None:
        from .mesh_convert import MeshData
        from .mesh_repack import build_submesh_info, finish_mesh

        container, path_id = key
        original, raw, endian = self.original(container, path_id)
        submesh_total = len(original.submesh_indices)
        claimed: set[int] = set()
        placed: list[tuple[Any, numpy.ndarray, list[int], int, int]] = []
        fallback: list[tuple[int, int]] = []
        emptied: set[int] = set()
        for mesh_index, owner in props:
            renderer_container, renderer_obj, renderer = self.prop_renderer(mesh_index, owner)
            subset = [int(value) for value in renderer.get("m_SubsetIndices") or []]
            primitives = self.meshes[mesh_index].get("primitives") or []
            root = renderer.get("m_StaticBatchRoot") or {}
            valid = bool(subset) and all(0 <= value < submesh_total for value in subset)
            if not valid or len(primitives) != len(subset) or any(value in claimed for value in subset) or int(root.get("m_FileID") or 0) != 0:
                fallback.append((mesh_index, owner))
                if valid:
                    emptied.update(value for value in subset if value not in claimed)
                continue
            for primitive in primitives:
                if isinstance(primitive.get("extras"), dict):
                    primitive["extras"].pop("unitySubmeshIndex", None)
            imported = self.imported(mesh_index, original)
            if len(imported.submesh_indices) != len(subset):
                fallback.append((mesh_index, owner))
                emptied.update(subset)
                continue
            matrix = self.node_world(owner)
            if int(root.get("m_PathID") or 0):
                matrix = numpy.linalg.inv(self.transform_world(container, int(root["m_PathID"]))) @ matrix
            claimed.update(subset)
            placed.append((imported, matrix, subset, mesh_index, owner))
        while True:
            combined = assemble_batch(original, placed, emptied, submesh_total)
            if len(combined[0]) <= MESH_VERTEX_LIMIT or not placed:
                break
            largest = max(range(len(placed)), key=lambda at: len(placed[at][0].vertices))
            fallback.append(placed[largest][3:5])
            emptied.update(placed[largest][2])
            del placed[largest]
        if len(combined[0]) > MESH_VERTEX_LIMIT:
            raise GlbEditError(f"static batch {original.name} in {container} is still over {MESH_VERTEX_LIMIT} vertices")
        if placed or emptied:
            vertices, normals, tangents, colors, uv_layers, submesh_indices = combined
            mesh = MeshData(
                name=original.name,
                vertices=vertices,
                indices=[index for submesh in submesh_indices for index in submesh],
                normals=normals,
                uv_layers=uv_layers,
                tangents=tangents,
                colors=colors,
                submeshes=build_submesh_info(vertices, submesh_indices),
                submesh_indices=submesh_indices,
                bind_poses=[],
                bone_name_hashes=[],
                skin=[],
                blend_shape_vertices=[],
                blend_shape_frames=[],
                blend_shape_channels=[],
                blend_shape_weights=[],
                root_bone_name_hash=original.root_bone_name_hash,
            )
            obj = self.current(container, path_id)[0]
            self.stage(container, obj, finish_mesh(mesh, original, raw, endian), original.name)
            self.result.batches += 1
            self.result.batched_props += len(placed)
        for mesh_index, owner in fallback:
            self.split_out(mesh_index, owner, container, path_id)

    def split_out(self, mesh_index: int, owner: int, container: str, path_id: int) -> None:
        name = str(self.meshes[mesh_index].get("name") or self.nodes[owner].get("name") or "Mesh")
        owner_container, components = self.owner_components(owner)
        filter_entry = next((component for component in components if component[0] == MESH_FILTER and component[1] == 0), None)
        if filter_entry is None:
            raise GlbEditError(f'node "{self.nodes[owner].get("name")}" has no MeshFilter in its own file to point at its split out mesh')
        renderer_container, renderer_obj, renderer = self.prop_renderer(mesh_index, owner)
        if encode_renderer(decode_renderer(self.games.get(renderer_container, renderer_obj.path_id)[1])) != self.games.get(renderer_container, renderer_obj.path_id)[1]:
            raise GlbEditError(f'the renderer of "{self.nodes[owner].get("name")}" doesnt rebuild byte for byte, so it cant be split out of its static batch')
        new_id = self.allocate(f"{self.source}#split{renderer_obj.path_id}", 1, owner_container)
        self.add_mesh(owner_container, new_id, self.mesh_payload(mesh_index, container, path_id), name)
        filter_obj, filter_raw = self.current(owner_container, filter_entry[2])
        data = bytearray(filter_raw)
        struct.pack_into("<iq", data, 12, 0, new_id)
        self.stage(owner_container, filter_obj, bytes(data), name)
        renderer["m_SubsetIndices"] = []
        renderer["m_StaticBatchRoot"] = {"m_FileID": 0, "m_PathID": 0}
        self.stage(renderer_container, renderer_obj, encode_renderer(renderer), name)
        self.result.split += 1

def keep_lengths(turned: numpy.ndarray, source: numpy.ndarray) -> numpy.ndarray:
    lengths = numpy.linalg.norm(turned, axis=1, keepdims=True)
    return numpy.divide(turned * numpy.linalg.norm(source, axis=1, keepdims=True), lengths, out=numpy.zeros_like(turned), where=lengths > 0)

def unit_rows(turned: numpy.ndarray) -> numpy.ndarray:
    lengths = numpy.linalg.norm(turned, axis=1, keepdims=True)
    return numpy.divide(turned, lengths, out=numpy.zeros_like(turned), where=lengths > 0)

def assemble_batch(original: Any, placed: list[tuple[Any, numpy.ndarray, list[int], int, int]], emptied: set[int], submesh_total: int) -> tuple:
    count = len(original.vertices)
    has_normals = len(original.normals) == count
    has_tangents = len(original.tangents) == count
    has_colors = len(original.colors) == count
    layers = [index for index, layer in enumerate(original.uv_layers[:4]) if len(layer) == count]
    claimed = {submesh for entry in placed for submesh in entry[2]}
    columns: list[numpy.ndarray] = []
    submesh_indices: list[list[int]] = [[] for unused in range(submesh_total)]
    total = 0

    def add_block(rows: numpy.ndarray, parts: dict[int, list[int]]) -> None:
        nonlocal total
        if not len(rows):
            for submesh in parts:
                submesh_indices[submesh] = []
            return
        unique, first, inverse = numpy.unique(rows, axis=0, return_index=True, return_inverse=True)
        order = numpy.argsort(first)
        position = numpy.empty(len(order), dtype=numpy.int64)
        position[order] = numpy.arange(len(order))
        remap = position[inverse.reshape(-1)] + total
        columns.append(unique[order])
        total += len(order)
        for submesh, indices in parts.items():
            submesh_indices[submesh] = remap[numpy.asarray(indices, dtype=numpy.int64)].tolist() if indices else []

    def stack(points, normals, tangents, colors, uvs) -> numpy.ndarray:
        parts = [points]
        if has_normals:
            parts.append(normals)
        if has_tangents:
            parts.append(tangents)
        if has_colors:
            parts.append(colors)
        parts.extend(uvs)
        return numpy.concatenate(parts, axis=1) if len(points) else numpy.zeros((0, sum(part.shape[1] for part in parts)))

    kept = [submesh for submesh in range(submesh_total) if submesh not in claimed and submesh not in emptied]
    used = sorted({vertex for submesh in kept for vertex in original.submesh_indices[submesh]})
    local = {vertex: at for at, vertex in enumerate(used)}
    ids = numpy.asarray(used, dtype=numpy.int64)
    if len(ids):
        rows = stack(
            numpy.asarray(original.vertices, dtype=float).reshape(-1, 3)[ids],
            numpy.asarray(original.normals, dtype=float).reshape(-1, 3)[ids] if has_normals else None,
            numpy.asarray(original.tangents, dtype=float).reshape(-1, 4)[ids] if has_tangents else None,
            numpy.asarray(original.colors, dtype=float).reshape(-1, 4)[ids] if has_colors else None,
            [numpy.asarray(original.uv_layers[layer], dtype=float).reshape(-1, 2)[ids] * [1.0, -1.0] + [0.0, 1.0] for layer in layers],
        )
    else:
        rows = numpy.zeros((0, 0))
    add_block(rows, {submesh: [local[vertex] for vertex in original.submesh_indices[submesh]] for submesh in kept})
    for submesh in emptied - claimed:
        submesh_indices[submesh] = []
    for imported, matrix, subset, unused_mesh, unused_owner in placed:
        size = len(imported.vertices)
        points = numpy.asarray(imported.vertices, dtype=float).reshape(-1, 3)
        world = (matrix @ numpy.c_[points, numpy.ones(size)].T).T[:, :3] if size else points
        linear = matrix[:3, :3]
        normals = tangents = colors = None
        if has_normals:
            source = numpy.asarray(imported.normals, dtype=float).reshape(-1, 3) if len(imported.normals) == size else numpy.tile([0.0, 1.0, 0.0], (size, 1))
            turned = (numpy.linalg.inv(linear).T @ source.T).T
            normals = unit_rows(turned)
        if has_tangents:
            source = numpy.asarray(imported.tangents, dtype=float).reshape(-1, 4) if len(imported.tangents) == size else numpy.tile([1.0, 0.0, 0.0, 1.0], (size, 1))
            turned = (linear @ source[:, :3].T).T
            tangents = numpy.c_[unit_rows(turned), source[:, 3]]
        if has_colors:
            colors = numpy.asarray(imported.colors, dtype=float).reshape(-1, 4) if len(imported.colors) == size else numpy.ones((size, 4))
        uvs = [numpy.asarray(imported.uv_layers[layer], dtype=float).reshape(-1, 2) if layer < len(imported.uv_layers) and len(imported.uv_layers[layer]) == size else numpy.zeros((size, 2)) for layer in layers]
        add_block(stack(world, normals, tangents, colors, uvs), {submesh: list(imported.submesh_indices[slot]) for slot, submesh in enumerate(subset)})
    merged = numpy.concatenate(columns) if columns else numpy.zeros((0, 3))
    at = 3
    def rows_of(start: int, width: int) -> list[tuple]:
        return list(map(tuple, merged[:, start: start + width].tolist()))

    vertices = rows_of(0, 3)
    normals_out: list = []
    tangents_out: list = []
    colors_out: list = []
    if has_normals:
        normals_out = rows_of(at, 3)
        at += 3
    if has_tangents:
        tangents_out = rows_of(at, 4)
        at += 4
    if has_colors:
        colors_out = rows_of(at, 4)
        at += 4
    uv_layers: list[list[tuple[float, float]]] = [[] for unused in range(max(layers) + 1 if layers else 0)]
    for layer in layers:
        uv_layers[layer] = rows_of(at, 2)
        at += 2
    return vertices, normals_out, tangents_out, colors_out, uv_layers, submesh_indices
def add_new_nodes(document, binary, nodes, parents, reachable, scratch, games, current, stage, build_added, result, scene) -> None:
    from .asset_bundle_prefab import PREFAB_FORMAT
    from .prefab_from_glb import convert_position, convert_rotation, convert_scale, extract_mesh_glb

    roots = [index for index, node in enumerate(nodes) if index in reachable and node_tag(node) is None and not is_mesh_helper(nodes, parents, index, document, games) and (parents.get(index) is None or node_tag(nodes[parents[index]]) is not None)]
    if not roots:
        return
    folder = scratch / "added"
    folder.mkdir(parents=True, exist_ok=True)
    materials = document.get("materials") or []
    default_container = next((str(node_tag(node)["unity_container"]) for node in nodes if node_tag(node)), "")

    def spec(index: int, files: list[Path]) -> dict[str, Any]:
        node = nodes[index]
        if "matrix" in node:
            raise GlbEditError(f'added node "{node.get("name")}" stores a matrix, export it with separate location, rotation and scale')
        entry: dict[str, Any] = {
            "name": compiled_name(node) or "GameObject",
            "position": convert_position(tuple(node.get("translation") or (0.0, 0.0, 0.0))),
            "rotation": convert_rotation(tuple(node.get("rotation") or (0.0, 0.0, 0.0, 1.0))),
            "scale": convert_scale(tuple(node.get("scale") or (1.0, 1.0, 1.0))),
        }
        if node.get("mesh") is not None:
            if "skin" in node:
                raise GlbEditError(f'added node "{node.get("name")}" is skinned, add new skinned meshes as a new prefab instead')
            mesh_index = int(node["mesh"])
            mesh_file = folder / f"added_{index}_mesh.glb"
            mesh_file.write_bytes(extract_mesh_glb(document, binary, mesh_index))
            files.append(mesh_file)
            slots = []
            for primitive in document["meshes"][mesh_index].get("primitives") or []:
                material_index = primitive.get("material")
                extras = (materials[int(material_index)].get("extras") or {}) if material_index is not None and 0 <= int(material_index) < len(materials) else {}
                if extras.get("unity_path_id") is not None and extras.get("unity_container"):
                    slots.append({"game": str(materials[int(material_index)].get("name") or ""), "id": int(extras["unity_path_id"]), "in": str(extras["unity_container"])})
                else:
                    raise GlbEditError(f'added node "{node.get("name")}" needs one of the existing materials in this GLB')
            entry["components"] = [{"type": "MeshFilter", "mesh": mesh_file.name}, {"type": "MeshRenderer", "materials": slots}]
        children = [spec(int(child), files) for child in node.get("children") or []]
        if children:
            entry["children"] = children
        return entry

    for root in roots:
        parent_index = parents.get(root)
        if parent_index is None and not scene:
            raise GlbEditError(f'"{nodes[root].get("name")}" was added outside the prefab root, put it under one of the prefabs nodes')
        parent_tag = node_tag(nodes[parent_index]) if parent_index is not None else None
        container = str(parent_tag["unity_container"]) if parent_tag else default_container
        files: list[Path] = []
        document_spec = {PREFAB_FORMAT_KEY: PREFAB_FORMAT, "name": compiled_name(nodes[root]) or "Added", "root": spec(root, files)}
        prefab = folder / f"added_{root}.prefab.json"
        prefab.write_text(json.dumps(document_spec, indent=2), encoding="utf-8")
        built, by_file = build_added([prefab] + files, container)
        root_go = by_file.get(prefab.name.lower())
        transforms = [item for item in built if item.class_id == TRANSFORM and item.payload is not None and read_transform(item.payload).father == (0, 0)]
        root_transform = next((item for item in transforms if struct.unpack_from("<iq", item.payload, 0) == (0, root_go[0])), None) if root_go else None
        if root_transform is None:
            raise GlbEditError(f'couldnt find the root of added node "{nodes[root].get("name")}" after building it')
        if parent_tag is not None:
            root_transform.payload = with_father(root_transform.payload, (0, int(parent_tag["unity_transform_path_id"])))
            parent_obj, parent_raw = current(container, parent_tag["unity_transform_path_id"])
            stage(container, parent_obj, with_children(parent_raw, read_transform(parent_raw).children + [(0, root_transform.path_id)]), str(nodes[parent_index].get("name") or ""))
        result.objects.extend(built)
        result.added += 1

def hide_missing(nodes, present, games, current, stage, result, scene) -> None:
    for node in nodes:
        tag = node_tag(node)
        if tag is None:
            continue
        container = str(tag["unity_container"])
        found = games.get(container, tag["unity_transform_path_id"])
        if found is None:
            continue
        for file_id, child in read_transform(found[1]).children:
            if file_id == 0 and child not in present.get(container.lower(), set()):
                switch_off(container, child, games, current, stage, result)
    if not scene:
        return
    for container_key, ids in present.items():
        container = next(str(node_tag(node)["unity_container"]) for node in nodes if node_tag(node) and str(node_tag(node)["unity_container"]).lower() == container_key)
        asset, by_id = games.asset(container)
        for obj in asset.objects:
            if obj.class_id not in (TRANSFORM, 224) or obj.path_id in ids:
                continue
            found = games.get(container, obj.path_id)
            if found is not None and read_transform(found[1]).father == (0, 0):
                switch_off(container, obj.path_id, games, current, stage, result)

def switch_off(container: str, transform_id: int, games, current, stage, result) -> None:
    found = games.get(container, transform_id)
    if found is None:
        return
    owner = struct.unpack_from("<iq", found[1], 0)
    if owner[0] != 0:
        return
    game_object = games.get(container, owner[1])
    if game_object is None or game_object[0].class_id != GAME_OBJECT:
        return
    raw = current(container, owner[1])[1]
    unused, unused_at, active_offset, unused_name = game_object_parts(raw)
    if active_offset >= len(raw) or raw[active_offset] == 0:
        return
    data = bytearray(raw)
    data[active_offset] = 0
    stage(container, game_object[0], bytes(data), "")
    result.hidden += 1
