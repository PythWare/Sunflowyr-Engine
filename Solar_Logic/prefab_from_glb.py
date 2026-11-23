from __future__ import annotations
import json, struct
from pathlib import Path
from typing import Any
from .animation_glb_import import AnimationGlbImportError, read_glb

GLB_MAGIC = 0x46546C67
JSON_CHUNK = 0x4E4F534A
BIN_CHUNK = 0x004E4942
PREFAB_FORMAT = "sunflowyr-new-prefab"

class PrefabFromGlbError(ValueError):
    pass

def convert_position(values: tuple[float, float, float]) -> dict[str, float]:
    return {"x": -float(values[0]), "y": float(values[1]), "z": float(values[2])}

def convert_rotation(values: tuple[float, float, float, float]) -> dict[str, float]:
    return {
        "x": float(values[0]),
        "y": -float(values[1]),
        "z": -float(values[2]),
        "w": float(values[3]),
    }

def convert_scale(values: tuple[float, float, float]) -> dict[str, float]:
    return {"x": float(values[0]), "y": float(values[1]), "z": float(values[2])}

def is_hierarchy_glb(document: dict[str, Any]) -> bool:
    if is_animation_glb(document):
        return False
    for node in document.get("nodes") or []:
        extras = node.get("extras") or {}
        if "unity_transform_path_id" in extras or "unity_game_object_path_id" in extras:
            return True
    return False

def is_animation_glb(document: dict[str, Any]) -> bool:
    for animation in document.get("animations") or []:
        extras = animation.get("extras") or {}
        if "unity_animation_clip_path_id" in extras:
            return True
    return False

def safe_name(value: str, fallback: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in " -_." else "_" for c in str(value or "")).strip(" .")
    return cleaned[:96] or fallback

def pad(data: bytes, alignment: int, filler: bytes) -> bytes:
    remainder = (-len(data)) % alignment
    return data + filler * remainder

def build_glb(document: dict[str, Any], binary: bytes) -> bytes:
    json_chunk = pad(json.dumps(document, separators=(",", ":")).encode("utf-8"), 4, b" ")
    bin_chunk = pad(binary, 4, b"\x00")
    total = 12 + 8 + len(json_chunk) + (8 + len(bin_chunk) if bin_chunk else 0)
    out = struct.pack("<III", GLB_MAGIC, 2, total)
    out += struct.pack("<II", len(json_chunk), JSON_CHUNK) + json_chunk
    if bin_chunk:
        out += struct.pack("<II", len(bin_chunk), BIN_CHUNK) + bin_chunk
    return out

def joint_bone_indices(document: dict[str, Any], bone_keys: list[tuple[str, int] | None]) -> dict[int, int]:
    wanted = {(str(key[0]).replace("\\", "/").lower(), int(key[1])): index for index, key in enumerate(bone_keys) if key is not None}
    found: dict[int, int] = {}
    for node_index, node in enumerate(document.get("nodes") or []):
        extras = node.get("extras") if isinstance(node, dict) else None
        if isinstance(extras, dict) and extras.get("unity_container") and extras.get("unity_transform_path_id") is not None:
            index = wanted.get((str(extras["unity_container"]).replace("\\", "/").lower(), int(extras["unity_transform_path_id"])))
            if index is not None:
                found[node_index] = index
    return found

def skinned_owners(document: dict[str, Any]) -> dict[int, dict[str, Any]]:
    owners: dict[int, dict[str, Any]] = {}
    for node in document.get("nodes") or []:
        if isinstance(node, dict) and "skin" in node and node.get("mesh") is not None:
            owners.setdefault(int(node["mesh"]), node)
    return owners

def extract_mesh_glb(document: dict[str, Any], binary: bytes, mesh_index: int, bone_indices: dict[int, int] | None = None, owners: dict[int, dict[str, Any]] | None = None) -> bytes:
    meshes = document.get("meshes") or []
    if not 0 <= mesh_index < len(meshes):
        raise PrefabFromGlbError(f"mesh {mesh_index} is out of range")
    source_mesh = meshes[mesh_index]
    accessors = document.get("accessors") or []
    views = document.get("bufferViews") or []

    new_accessors: list[dict[str, Any]] = []
    new_views: list[dict[str, Any]] = []
    new_binary = bytearray()
    remap: dict[int, int] = {}

    def copy_accessor(index: int) -> int:
        index = int(index)
        if index in remap:
            return remap[index]
        if not 0 <= index < len(accessors):
            raise PrefabFromGlbError(f"accessor {index} is out of range")
        accessor = dict(accessors[index])
        view_index = accessor.pop("bufferView", None)
        if view_index is not None:
            view = views[int(view_index)]
            component_size = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}[
                int(accessor["componentType"])
            ]
            parts = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}[str(accessor["type"])]
            element = component_size * parts
            stride = int(view.get("byteStride") or element)
            start = int(view.get("byteOffset", 0)) + int(accessor.get("byteOffset", 0))
            count = int(accessor.get("count", 0))
            if stride == element:
                packed = bytes(binary[start : start + element * count])
            else:
                packed = bytearray()
                for item in range(count):
                    at = start + item * stride
                    packed.extend(binary[at : at + element])
            while len(new_binary) % 4:
                new_binary.append(0)
            offset = len(new_binary)
            new_binary.extend(packed)
            new_views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(packed)})
            accessor["bufferView"] = len(new_views) - 1
            accessor.pop("byteOffset", None)
        new_accessors.append(accessor)
        remap[index] = len(new_accessors) - 1
        return remap[index]

    primitives: list[dict[str, Any]] = []
    for primitive in source_mesh.get("primitives") or []:
        entry: dict[str, Any] = {"mode": primitive.get("mode", 4)}
        attributes = {}
        for name, accessor_index in (primitive.get("attributes") or {}).items():
            attributes[name] = copy_accessor(accessor_index)
        entry["attributes"] = attributes
        if primitive.get("indices") is not None:
            entry["indices"] = copy_accessor(primitive["indices"])
        if primitive.get("targets"):
            entry["targets"] = [{name: copy_accessor(index) for name, index in target.items()} for target in primitive["targets"]]
        if primitive.get("extras"):
            entry["extras"] = primitive["extras"]
        primitives.append(entry)

    mesh_entry: dict[str, Any] = {"name": source_mesh.get("name") or f"Mesh{mesh_index}", "primitives": primitives}
    if source_mesh.get("weights"):
        mesh_entry["weights"] = list(source_mesh["weights"])
    out_nodes: list[dict[str, Any]] = [{"name": source_mesh.get("name") or f"Mesh{mesh_index}", "mesh": 0}]
    out_skins: list[dict[str, Any]] = []
    source_nodes = document.get("nodes") or []
    owner = (owners if owners is not None else skinned_owners(document)).get(mesh_index)
    skins = document.get("skins") or []
    if owner is not None and 0 <= int(owner["skin"]) < len(skins):
        skin = skins[int(owner["skin"])]
        joints = []
        placed: dict[int, int] = {}
        slot_map: dict[int, int] = {}
        for bone_index, node_index in enumerate(skin.get("joints") or []):
            source_node = source_nodes[int(node_index)] if 0 <= int(node_index) < len(source_nodes) else {}
            extras = {key: value for key, value in (source_node.get("extras") or {}).items() if not str(key).startswith("unity_")}
            used = bone_indices is None or int(node_index) in bone_indices
            if bone_indices is not None and used:
                extras["unityBoneIndex"] = bone_indices[int(node_index)]
            if used:
                extras.setdefault("unityBoneIndex", bone_index)
            else:
                extras.pop("unityBoneIndex", None)
            joint = {"name": source_node.get("name") or f"Bone_{bone_index}", "extras": extras}
            for key in ("translation", "rotation", "scale", "matrix"):
                if key in source_node:
                    joint[key] = source_node[key]
            out_nodes.append(joint)
            placed[int(node_index)] = len(out_nodes) - 1
            if used:
                slot_map[bone_index] = len(joints)
                joints.append(len(out_nodes) - 1)
        for node_index, out_index in placed.items():
            children = [placed[int(child)] for child in source_nodes[node_index].get("children") or [] if int(child) in placed]
            if children:
                out_nodes[out_index]["children"] = children
        out_skin: dict[str, Any] = {"joints": joints}
        if skin.get("name"):
            out_skin["name"] = skin["name"]
        if skin.get("inverseBindMatrices") is not None:
            out_skin["inverseBindMatrices"] = copy_accessor(skin["inverseBindMatrices"])
        if len(slot_map) != len(skin.get("joints") or []):
            drop_unused_joints(primitives, out_skin, slot_map, new_accessors, new_views, new_binary, str(source_mesh.get("name") or mesh_index), [str(source_nodes[int(node)].get("name")) for node in skin.get("joints") or []])
        out_skins.append(out_skin)
        out_nodes[0]["skin"] = 0

    out_document: dict[str, Any] = {
        "asset": {"version": "2.0", "generator": "Sunflowyr Engine prefab-from-glb"},
        "scene": 0,
        "scenes": [{"nodes": [index for index in range(len(out_nodes)) if not any(index in (node.get("children") or []) for node in out_nodes)]}],
        "nodes": out_nodes,
        "meshes": [mesh_entry],
        "accessors": new_accessors,
        "bufferViews": new_views,
        "buffers": [{"byteLength": len(new_binary)}],
    }
    if out_skins:
        out_document["skins"] = out_skins
    if source_mesh.get("extras"):
        out_document["meshes"][0]["extras"] = source_mesh["extras"]
    return build_glb(out_document, bytes(new_binary))

def drop_unused_joints(primitives: list[dict[str, Any]], skin: dict[str, Any], slot_map: dict[int, int], accessors: list[dict[str, Any]], views: list[dict[str, Any]], binary: bytearray, mesh_name: str, joint_names: list[str]) -> None:
    component = {5121: "B", 5123: "H", 5125: "I"}
    done: set[int] = set()
    for primitive in primitives:
        attributes = primitive.get("attributes") or {}
        for set_index in range(8):
            joints_key, weights_key = f"JOINTS_{set_index}", f"WEIGHTS_{set_index}"
            if joints_key not in attributes:
                break
            joints_index = int(attributes[joints_key])
            if joints_index in done:
                continue
            done.add(joints_index)
            joints_accessor = accessors[joints_index]
            code = component[int(joints_accessor["componentType"])]
            view = views[int(joints_accessor["bufferView"])]
            count = int(joints_accessor["count"])
            values = list(struct.unpack_from(f"<{count * 4}{code}", binary, int(view["byteOffset"])))
            weights: list[float] = [1.0] * len(values)
            if weights_key in attributes:
                weight_accessor = accessors[int(attributes[weights_key])]
                weight_view = views[int(weight_accessor["bufferView"])]
                if int(weight_accessor["componentType"]) == 5126:
                    weights = list(struct.unpack_from(f"<{count * 4}f", binary, int(weight_view["byteOffset"])))
                else:
                    raw = struct.unpack_from(f"<{count * 4}{component[int(weight_accessor['componentType'])]}", binary, int(weight_view["byteOffset"]))
                    weights = [float(value) for value in raw]
            for at, slot in enumerate(values):
                if slot in slot_map:
                    values[at] = slot_map[slot]
                elif weights[at] > 0:
                    name = joint_names[slot] if slot < len(joint_names) else str(slot)
                    raise PrefabFromGlbError(f'mesh "{mesh_name}" has weights on "{name}", which isnt one of its bones in the game')
                else:
                    values[at] = 0
            struct.pack_into(f"<{count * 4}{code}", binary, int(view["byteOffset"]), *values)
    matrices_index = skin.get("inverseBindMatrices")
    if matrices_index is not None:
        accessor = accessors[int(matrices_index)]
        view = views[int(accessor["bufferView"])]
        rows = [bytes(binary[int(view["byteOffset"]) + slot * 64: int(view["byteOffset"]) + slot * 64 + 64]) for slot in sorted(slot_map, key=slot_map.get)]
        while len(binary) % 4:
            binary.append(0)
        views.append({"buffer": 0, "byteOffset": len(binary), "byteLength": 64 * len(rows)})
        binary.extend(b"".join(rows))
        accessors.append({"bufferView": len(views) - 1, "componentType": 5126, "count": len(rows), "type": "MAT4"})
        skin["inverseBindMatrices"] = len(accessors) - 1

def node_children(document: dict[str, Any]) -> dict[int, list[int]]:
    return {index: [int(c) for c in (node.get("children") or [])]
            for index, node in enumerate(document.get("nodes") or [])}

def scene_roots(document: dict[str, Any]) -> list[int]:
    scenes = document.get("scenes") or []
    index = int(document.get("scene", 0))
    if 0 <= index < len(scenes):
        roots = [int(item) for item in scenes[index].get("nodes", [])]
        if roots:
            return roots
    claimed = {c for children in node_children(document).values() for c in children}
    return [i for i in range(len(document.get("nodes") or [])) if i not in claimed]

def convert_hierarchy_glb(
    glb_path: Path,
    out_folder: Path,
    prefab_name: str | None = None,
    context: Any | None = None,
) -> dict[str, Any]:
    glb_path = Path(glb_path)
    try:
        document, binary = read_glb(glb_path.read_bytes())
    except AnimationGlbImportError as exc:
        raise PrefabFromGlbError(str(exc)) from exc
    nodes = document.get("nodes") or []
    if not nodes:
        raise PrefabFromGlbError("the glTF file has no nodes")

    out_folder = Path(out_folder)
    out_folder.mkdir(parents=True, exist_ok=True)
    meshes = document.get("meshes") or []
    materials = document.get("materials") or []

    mesh_files: dict[int, str] = {}
    used_names: set[str] = set()
    for index in sorted({int(node["mesh"]) for node in nodes if node.get("mesh") is not None}):
        base = safe_name(meshes[index].get("name") if index < len(meshes) else "", f"Mesh{index}")
        name = base
        counter = 2
        while name.casefold() in used_names:
            name = f"{base}_{counter}"
            counter += 1
        used_names.add(name.casefold())
        payload = extract_mesh_glb(document, binary, index)
        (out_folder / f"{name}.glb").write_bytes(payload)
        mesh_files[index] = f"{name}.glb"

    children = node_children(document)
    materials_seen: dict[int, Any] = {}
    warnings: list[str] = []
    clone_result = None
    written_assets: dict[Any, str] = {}
    if context is not None:
        from .prefab_clone import CloneResult

        clone_result = CloneResult(
            prefab_path=out_folder / "unused.prefab.json",
            folder=out_folder,
            root_name=prefab_name or glb_path.stem,
        )

    def material_reference(primitive: dict[str, Any]) -> str | None:
        index = primitive.get("material")
        if index is None or not 0 <= int(index) < len(materials):
            return None
        index = int(index)
        if index in materials_seen:
            return materials_seen[index]
        material = materials[index]
        extras = material.get("extras") or {}
        container = extras.get("unity_container")
        path_id = extras.get("unity_path_id")
        file_name = None
        if context is not None and container and path_id is not None:
            from .prefab_clone import export_material

            try:
                file_name = export_material(
                    context, (str(container), int(path_id)), out_folder, clone_result, written_assets
                )
            except Exception as exc:
                warnings.append(f'material "{material.get("name")}" couldnt be rebuilt: {exc}')
                file_name = None
        if file_name is None:
            warnings.append(
                f'material "{material.get("name")}" has no resolvable game source; '
                "its renderer slot will be empty"
            )
        materials_seen[index] = file_name
        return file_name

    def build_node(index: int) -> dict[str, Any]:
        node = nodes[index]
        extras = node.get("extras") or {}
        entry: dict[str, Any] = {
            "name": node.get("name") or f"Node{index}",
            "layer": int(extras.get("unity_layer", 0)),
            "tag": int(extras.get("unity_tag", 0)),
            "active": bool(extras.get("unity_active", True)),
            "position": convert_position(tuple(node.get("translation") or (0.0, 0.0, 0.0))),
            "rotation": convert_rotation(tuple(node.get("rotation") or (0.0, 0.0, 0.0, 1.0))),
            "scale": convert_scale(tuple(node.get("scale") or (1.0, 1.0, 1.0))),
            "components": [],
        }
        source = {}
        if extras.get("unity_container"):
            source["container"] = extras["unity_container"]
        if extras.get("unity_game_object_path_id") is not None:
            source["gameObjectPathId"] = extras["unity_game_object_path_id"]
        if extras.get("unity_transform_path_id") is not None:
            source["transformPathId"] = extras["unity_transform_path_id"]
        if source:
            entry["source"] = source
        mesh_index = node.get("mesh")
        if mesh_index is not None and int(mesh_index) in mesh_files:
            mesh_index = int(mesh_index)
            entry["components"].append(
                {"type": "MeshFilter", "fields": {"m_Mesh": {"asset": mesh_files[mesh_index]}}}
            )
            slots = []
            for primitive in (meshes[mesh_index].get("primitives") or []):
                reference = material_reference(primitive)
                if reference is not None:
                    slots.append({"asset": reference})
            renderer = {
                "type": "MeshRenderer",
                "fields": {
                    "m_Enabled": True,
                    "m_CastShadows": 1,
                    "m_ReceiveShadows": True,
                    "m_Materials": slots,
                },
            }
            entry["components"].append(renderer)
        child_entries = [build_node(child) for child in children.get(index, [])]
        if child_entries:
            entry["children"] = child_entries
        return entry

    roots = scene_roots(document)
    if len(roots) == 1:
        root = build_node(roots[0])
    else:
        root = {
            "name": prefab_name or glb_path.stem,
            "layer": 0,
            "tag": 0,
            "active": True,
            "position": {"x": 0.0, "y": 0.0, "z": 0.0},
            "rotation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
            "scale": {"x": 1.0, "y": 1.0, "z": 1.0},
            "components": [],
            "children": [build_node(index) for index in roots],
        }
    name = prefab_name or root.get("name") or glb_path.stem
    prefab = {"format": PREFAB_FORMAT, "name": name, "root": root}
    prefab_path = out_folder / f"{safe_name(name, 'Prefab')}.prefab.json"
    prefab_path.write_text(json.dumps(prefab, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")

    def count_nodes(entry: dict[str, Any]) -> int:
        return 1 + sum(count_nodes(child) for child in entry.get("children") or [])

    return {
        "prefab": str(prefab_path),
        "folder": str(out_folder),
        "nodes": count_nodes(root),
        "meshes": len(mesh_files),
        "materials": sum(1 for value in materials_seen.values() if value),
        "warnings": warnings + list(getattr(clone_result, "warnings", None) or []),
        "sourceGlb": str(glb_path),
    }

def root_source_key(document: dict[str, Any]) -> tuple[str, int] | None:
    nodes = document.get("nodes") or []
    for index in scene_roots(document):
        if not 0 <= index < len(nodes):
            continue
        extras = nodes[index].get("extras") or {}
        container = extras.get("unity_container")
        path_id = extras.get("unity_game_object_path_id")
        if container and path_id is not None:
            return (str(container), int(path_id))
    return None

def index_glb_nodes(document: dict[str, Any]) -> tuple[dict[int, dict[str, Any]], dict[int, int]]:
    by_game_object: dict[int, dict[str, Any]] = {}
    mesh_by_game_object: dict[int, int] = {}
    for node in document.get("nodes") or []:
        extras = node.get("extras") or {}
        path_id = extras.get("unity_game_object_path_id")
        if path_id is None:
            continue
        by_game_object[int(path_id)] = node
        if node.get("mesh") is not None:
            mesh_by_game_object[int(path_id)] = int(node["mesh"])
    return by_game_object, mesh_by_game_object

def apply_glb_edits_to_prefab(
    prefab: dict[str, Any],
    document: dict[str, Any],
    binary: bytes | None = None,
    out_folder: Path | None = None,
    written: dict[Any, str] | None = None,
) -> dict[str, Any]:
    nodes = document.get("nodes") or []
    stats: dict[str, Any] = {
        "transformsUpdated": 0,
        "nodesMatched": 0,
        "nodesMissingFromGlb": 0,
        "nodesRenamed": 0,
        "nodesAdded": 0,
        "meshesAdded": 0,
        "materialsUnresolved": [],
    }

    glb_by_path_id: dict[int, int] = {}
    for index, node in enumerate(nodes):
        extras = node.get("extras") or {}
        path_id = extras.get("unity_game_object_path_id")
        if path_id is not None:
            glb_by_path_id.setdefault(int(path_id), index)

    prefab_root = prefab.get("root")
    if not isinstance(prefab_root, dict):
        return stats

    prefab_by_path_id: dict[int, dict[str, Any]] = {}
    original_parent: dict[int, dict[str, Any] | None] = {}
    original_children: dict[int, list] = {}
    nodes_by_id: dict[int, dict[str, Any]] = {}

    def index_prefab(node: dict[str, Any], parent: dict[str, Any] | None) -> None:
        original_parent[id(node)] = parent
        original_children[id(node)] = list(node.get("children") or [])
        nodes_by_id[id(node)] = node
        source = node.get("source") or {}
        path_id = source.get("gameObjectPathId")
        if path_id is not None:
            prefab_by_path_id.setdefault(int(path_id), node)
        for child in node.get("children") or []:
            if isinstance(child, dict):
                index_prefab(child, node)

    index_prefab(prefab_root, None)
    old_paths = node_paths(prefab_root)

    root_source = prefab_root.get("source") or {}
    root_path_id = root_source.get("gameObjectPathId")
    root_index = glb_by_path_id.get(int(root_path_id)) if root_path_id is not None else None
    if root_index is None:
        return apply_glb_transforms_only(prefab_root, glb_by_path_id, nodes, stats)

    material_files: dict[tuple[str, int], str] = {}
    for key, file_name in (written or {}).items():
        if isinstance(key, tuple) and len(key) == 2:
            material_files[(str(key[0]).replace(chr(92), "/").lower(), int(key[1]))] = file_name
    can_write_meshes = binary is not None and out_folder is not None

    def resolve_materials(mesh_index: int) -> tuple[list[str], list[str]]:
        resolved: list[str] = []
        unresolved: list[str] = []
        meshes = document.get("meshes") or []
        materials = document.get("materials") or []
        if not 0 <= mesh_index < len(meshes):
            return resolved, unresolved
        for primitive in meshes[mesh_index].get("primitives") or []:
            material_index = primitive.get("material")
            if material_index is None or not 0 <= int(material_index) < len(materials):
                unresolved.append("(no material)")
                continue
            material = materials[int(material_index)]
            extras = material.get("extras") or {}
            container = extras.get("unity_container")
            path_id = extras.get("unity_path_id")
            file_name = None
            if container and path_id is not None:
                file_name = material_files.get(
                    (str(container).replace(chr(92), "/").lower(), int(path_id))
                )
            if file_name:
                resolved.append(file_name)
            else:
                unresolved.append(str(material.get("name") or "material %d" % material_index))
        return resolved, unresolved

    def added_node(node: dict[str, Any]) -> dict[str, Any]:
        name = str(node.get("name") or "GameObject")
        entry: dict[str, Any] = {"name": name, "addedFromGlb": True}
        mesh_index = node.get("mesh")
        if mesh_index is None or not can_write_meshes:
            return entry
        file_name = new_mesh_file_name(name, int(mesh_index))
        try:
            (out_folder / file_name).write_bytes(
                extract_mesh_glb(document, binary, int(mesh_index))
            )
        except (PrefabFromGlbError, OSError):
            return entry
        resolved, unresolved = resolve_materials(int(mesh_index))
        stats["meshesAdded"] += 1
        for item in unresolved:
            stats["materialsUnresolved"].append("%s: %s" % (name, item))
        entry["components"] = [
            {"type": "MeshFilter", "mesh": file_name},
            {"type": "MeshRenderer", "materials": resolved},
        ]
        return entry

    used: set[int] = set()

    def rebuild(index: int) -> dict[str, Any]:
        node = nodes[index]
        extras = node.get("extras") or {}
        path_id = extras.get("unity_game_object_path_id")
        target = prefab_by_path_id.get(int(path_id)) if path_id is not None else None
        if target is None:
            target = added_node(node)
            stats["nodesAdded"] += 1
        else:
            from .glb_edits import compiled_name

            used.add(id(target))
            stats["nodesMatched"] += 1
            name = compiled_name(node)
            if name is not None and name != str(target.get("name") or ""):
                target.setdefault("pathName", str(target.get("name") or "GameObject"))
                target["name"] = name
                stats["nodesRenamed"] += 1
        if apply_transform(target, node):
            stats["transformsUpdated"] += 1
        target["children"] = [
            rebuild(int(child))
            for child in node.get("children") or []
            if 0 <= int(child) < len(nodes)
        ]
        return target

    rebuilt_root = rebuild(root_index)
    if rebuilt_root is not prefab_root:
        used.add(id(prefab_root))
        prefab_root["children"] = [rebuilt_root]

    for node_id, node in nodes_by_id.items():
        if node_id in used:
            continue
        stats["nodesMissingFromGlb"] += 1
        node["children"] = [
            child for child in original_children.get(node_id, []) if id(child) not in used
        ]
    for node_id, parent in original_parent.items():
        if node_id in used or parent is None or id(parent) not in used:
            continue
        node = nodes_by_id[node_id]
        siblings = parent.setdefault("children", [])
        if not any(item is node for item in siblings):
            siblings.append(node)

    for node in nodes_by_id.values():
        if not node.get("children"):
            node.pop("children", None)
    new_paths = node_paths(prefab_root)
    stats["movedPaths"] = {old_paths[key]: path for key, path in new_paths.items() if key in old_paths and old_paths[key] != path}
    return stats

def node_paths(root: dict[str, Any]) -> dict[int, str]:
    found: dict[int, str] = {}

    def walk(node: dict[str, Any], parent: str) -> None:
        key = str(node.get("pathName") or node.get("name") or "GameObject")
        path = f"{parent}/{key}" if parent else key
        found.setdefault(id(node), path)
        for child in node.get("children") or []:
            if isinstance(child, dict):
                walk(child, path)

    walk(root, "")
    return found

def rewrite_node_references(value: Any, moved: dict[str, str], prefab_file: str, inside_prefab: bool) -> int:
    changed = 0
    if isinstance(value, dict):
        target = value.get("node")
        named = str(value.get("prefab") or "")
        own = named.casefold() == prefab_file.casefold() or (inside_prefab and not named)
        if isinstance(target, str) and own and target in moved:
            value["node"] = moved[target]
            changed += 1
        for item in value.values():
            changed += rewrite_node_references(item, moved, prefab_file, inside_prefab)
    elif isinstance(value, list):
        for item in value:
            changed += rewrite_node_references(item, moved, prefab_file, inside_prefab)
    return changed

def new_mesh_file_name(node_name: str, mesh_index: int) -> str:
    safe = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in str(node_name)
    )
    return "%s__glb%d.glb" % (safe[:48] or "Mesh", mesh_index)

def apply_transform(target: dict[str, Any], glb_node: dict[str, Any]) -> bool:
    translation = glb_node.get("translation")
    rotation = glb_node.get("rotation")
    scale = glb_node.get("scale")
    changed = False
    if translation is not None:
        target["position"] = convert_position(tuple(translation))
        changed = True
    if rotation is not None:
        target["rotation"] = convert_rotation(tuple(rotation))
        changed = True
    if scale is not None:
        target["scale"] = convert_scale(tuple(scale))
        changed = True
    return changed

def apply_glb_transforms_only(
    prefab_root: dict[str, Any],
    glb_by_path_id: dict[int, int],
    nodes: list,
    stats: dict[str, Any],
) -> dict[str, Any]:

    def visit(node: dict[str, Any]) -> None:
        source = node.get("source") or {}
        path_id = source.get("gameObjectPathId")
        index = glb_by_path_id.get(int(path_id)) if path_id is not None else None
        if index is None:
            stats["nodesMissingFromGlb"] += 1
        else:
            stats["nodesMatched"] += 1
            if apply_transform(node, nodes[index]):
                stats["transformsUpdated"] += 1
        for child in node.get("children") or []:
            visit(child)

    visit(prefab_root)
    return stats

def replace_cloned_meshes(
    document: dict[str, Any],
    binary: bytes,
    written: dict[Any, str],
    out_folder: Path,
    context: Any | None = None,
) -> list[str]:
    replaced: list[str] = []
    meshes = document.get("meshes") or []
    renderers = {}
    if context is not None:
        for records in context.renderers_by_game_object.values():
            for record in records:
                renderers[(str(record.key[0]).replace("\\", "/").lower(), int(record.key[1]))] = record
    by_key = {}
    for key, file_name in written.items():
        if isinstance(key, tuple) and len(key) == 2:
            by_key[(str(key[0]).replace("\\", "/").lower(), int(key[1]))] = file_name
    for index, mesh in enumerate(meshes):
        extras = mesh.get("extras") or {}
        container = extras.get("unity_container")
        mesh_path_id = extras.get("unity_mesh_path_id")
        if not container or mesh_path_id is None or extras.get("static_batch"):
            continue
        file_name = by_key.get((str(container).replace("\\", "/").lower(), int(mesh_path_id)))
        if not file_name:
            continue
        target = out_folder / file_name
        if not target.is_file():
            continue
        bone_indices = None
        record = renderers.get((str(extras.get("unity_renderer_container") or container).replace("\\", "/").lower(), int(extras.get("unity_renderer_path_id") or 0)))
        skinned = any(node.get("mesh") == index and "skin" in node for node in document.get("nodes") or [])
        if skinned and record is not None and record.bones:
            bone_indices = joint_bone_indices(document, [context.resolve(record.asset_key, bone) for bone in record.bones])
        target.write_bytes(extract_mesh_glb(document, binary, index, bone_indices))
        replaced.append(file_name)
    return replaced

def split_batched_meshes(
    prefab: dict[str, Any],
    document: dict[str, Any],
    binary: bytes,
    out_folder: Path,
) -> tuple[list[str], set[str]]:
    meshes = document.get("meshes") or []
    batched: dict[int, int] = {}
    for node in document.get("nodes") or []:
        mesh_index = node.get("mesh")
        extras = node.get("extras") or {}
        if mesh_index is None or not 0 <= int(mesh_index) < len(meshes) or extras.get("unity_game_object_path_id") is None:
            continue
        if (meshes[int(mesh_index)].get("extras") or {}).get("static_batch"):
            batched[int(extras["unity_game_object_path_id"])] = int(mesh_index)
    if not batched:
        return [], set()
    split: list[str] = []
    dropped: set[str] = set()

    def visit(node: dict[str, Any]) -> None:
        path_id = (node.get("source") or {}).get("gameObjectPathId")
        mesh_index = batched.get(int(path_id)) if path_id is not None else None
        components = node.get("components") or []
        filters = [item for item in components if item.get("type") == "MeshFilter" and isinstance(item.get("fields"), dict)]
        renderers = [item for item in components if item.get("type") == "MeshRenderer" and isinstance(item.get("fields"), dict)]
        if mesh_index is not None and filters and renderers:
            file_name = new_mesh_file_name(str(node.get("name") or "Mesh"), mesh_index)
            (out_folder / file_name).write_bytes(extract_mesh_glb(document, binary, mesh_index))
            previous = filters[0]["fields"].get("m_Mesh")
            if isinstance(previous, dict) and isinstance(previous.get("asset"), str):
                dropped.add(previous["asset"])
            filters[0]["fields"]["m_Mesh"] = {"asset": file_name}
            renderers[0]["fields"]["m_SubsetIndices"] = []
            renderers[0]["fields"]["m_StaticBatchRoot"] = {"m_FileID": 0, "m_PathID": 0}
            split.append(file_name)
        for child in node.get("children") or []:
            visit(child)

    visit(prefab.get("root") or {})
    return split, dropped

def drop_unused_files(out_folder: Path, dropped: set[str]) -> None:
    texts = []
    for other in out_folder.glob("*.json"):
        try:
            texts.append(other.read_text(encoding="utf-8-sig"))
        except OSError:
            return
    for file_name in dropped:
        if not any(json.dumps(file_name) in text for text in texts) and (out_folder / file_name).is_file():
            (out_folder / file_name).unlink()

def rebuild_prefab_from_edited_glb(
    glb_path: Path,
    out_folder: Path,
    context: Any,
    prefab_name: str | None = None,
    managed_registry: Any | None = None,
    mono_scripts: dict | None = None,
    follow_refs: int = 0,
    game_refs: bool = False,
) -> dict[str, Any]:
    from .prefab_clone import PrefabCloneError, clone_character_exact

    glb_path = Path(glb_path)
    document, binary = read_glb(glb_path.read_bytes())
    root_key = root_source_key(document)
    if root_key is None:
        raise PrefabFromGlbError(
            "this GLB has no game prefab on its root node, use the geometry-only conversion instead"
        )
    written: dict[Any, str] = {}
    try:
        result = clone_character_exact(
            context,
            root_key,
            Path(out_folder),
            include_scripts=True,
            managed_registry=managed_registry,
            mono_scripts=mono_scripts,
            follow_refs=follow_refs,
            minimal=follow_refs == 0,
            written=written,
            game_refs=game_refs,
        )
    except PrefabCloneError as exc:
        raise PrefabFromGlbError(f"couldnt rebuild the original prefab: {exc}") from exc

    prefab_path = Path(result.prefab_path)
    prefab = json.loads(prefab_path.read_text(encoding="utf-8-sig"))
    stats = apply_glb_edits_to_prefab(prefab, document, binary, Path(out_folder), written)
    moved = stats.pop("movedPaths", {})
    if moved:
        rewrite_node_references(prefab, moved, prefab_path.name, True)
        for other in Path(out_folder).glob("*.json"):
            if other == prefab_path:
                continue
            try:
                other_document = json.loads(other.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if rewrite_node_references(other_document, moved, prefab_path.name, False):
                other.write_text(json.dumps(other_document, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
    if prefab_name:
        prefab["name"] = prefab_name
    split, dropped = split_batched_meshes(prefab, document, binary, Path(out_folder))
    prefab_path.write_text(json.dumps(prefab, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
    drop_unused_files(Path(out_folder), dropped)
    replaced = replace_cloned_meshes(document, binary, written, Path(out_folder), context) + split

    return {
        "mode": "clone-merge",
        "prefab": str(prefab_path),
        "folder": str(out_folder),
        "nodes": result.nodes,
        "meshes": result.meshes,
        "materials": result.materials,
        "scripts": len(result.scripts),
        "animations": result.animations,
        "transformsUpdated": stats["transformsUpdated"],
        "nodesMatched": stats["nodesMatched"],
        "nodesMissingFromGlb": stats["nodesMissingFromGlb"],
        "nodesRenamed": stats["nodesRenamed"],
        "nodesAdded": stats["nodesAdded"],
        "meshesAdded": stats["meshesAdded"],
        "materialsUnresolved": stats["materialsUnresolved"],
        "meshesReplaced": replaced,
        "warnings": list(result.warnings),
        "sourceGlb": str(glb_path),
    }
