from __future__ import annotations
import io, json, math, re
from array import array
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from . import export_log
from .binary import EndianReader
from .class_ids import class_name
from .gltf_writer import GL_FLOAT, GlbBuilder, Matrix
from .mesh_convert import MeshData, MeshDecodeError, read_mesh
from .mesh_glb_export import gltf_bind_pose
from .model_context import ModelContextIndex, ObjectKey, SceneMaterial, SceneTexture, TransformRecord
from .navmesh_export import read_navmesh_data, write_navmesh_sidecars
from .objects import describe_object
from .serialized import SerializedAsset, SerializedObject
from .terrain_export import TerrainDataRecord, read_terrain_data, write_terrain_sidecars
from .texture_convert import TextureDecodeError, decode_texture

ReconstructedKind = str
ReservePath = Callable[[Path, str], Path]

@dataclass(slots=True)
class ReconstructedResult:
    path: Path
    metadata: dict
    kind: ReconstructedKind
    component_manifest_path: Path | None = None
    component_manifest_metadata: dict | None = None
    terrain_json_path: Path | None = None
    terrain_json_metadata: dict | None = None
    navmesh_json_path: Path | None = None
    navmesh_json_metadata: dict | None = None
    auxiliary_files: list[tuple[Path, dict]] = field(default_factory=list)

@dataclass(slots=True)
class ReconstructedBatch:
    results: list[ReconstructedResult] = field(default_factory=list)
    errors: int = 0
    failures: list[tuple[str, str, str]] = field(default_factory=list)

    def count(self, kind: str) -> int:
        return sum(result.kind == kind for result in self.results)

    def fail(self, stage: str, source: str, error: BaseException) -> None:
        self.errors += 1
        self.failures.append((stage, source, export_log.describe(error)))

class ReconstructionSource:
    def __init__(self, context: ModelContextIndex, mesh_cache_size: int = 64, texture_cache_size: int = 64) -> None:
        self.context = context
        self.mesh_cache_size = mesh_cache_size
        self.texture_cache_size = texture_cache_size
        self.mesh_cache: OrderedDict[ObjectKey, MeshData] = OrderedDict()
        self.texture_cache: OrderedDict[ObjectKey, bytes | None] = OrderedDict()

    def mesh(self, key: ObjectKey) -> MeshData:
        cached = self.mesh_cache.pop(key, None)
        if cached is not None:
            self.mesh_cache[key] = cached
            return cached
        asset = self.context.assets.get(key[0])
        obj = self.context.objects.get(key)
        if asset is None or obj is None or obj.class_id != 43:
            raise MeshDecodeError(f"mesh object {key!r} was not found")
        with asset.path.open("rb") as handle:
            mesh = read_mesh(asset, EndianReader(handle, asset.endian), obj)
        self.mesh_cache[key] = mesh
        while len(self.mesh_cache) > self.mesh_cache_size:
            self.mesh_cache.popitem(last=False)
        return mesh

    def texture_png(self, texture: SceneTexture) -> bytes | None:
        key = texture.key
        if key in self.texture_cache:
            cached = self.texture_cache.pop(key)
            self.texture_cache[key] = cached
            return cached
        result = self.read_texture_png(key)
        self.texture_cache[key] = result
        while len(self.texture_cache) > self.texture_cache_size:
            self.texture_cache.popitem(last=False)
        return result

    def read_texture_png(self, key: ObjectKey) -> bytes | None:
        asset = self.context.assets.get(key[0])
        obj = self.context.objects.get(key)
        if asset is None or obj is None or obj.class_id != 28:
            return None
        try:
            with asset.path.open("rb") as handle:
                reader = EndianReader(handle, asset.endian)
                desc = describe_object(asset, reader, obj)
            payload = next((item for item in desc.payloads if item.kind == "texture2d"), None)
            if payload is None:
                return None
            resource = resolve_resource(asset, payload.source_path)
            if resource is None:
                return None
            with resource.open("rb") as handle:
                handle.seek(payload.source_offset)
                data = handle.read(payload.source_size)
            decoded = decode_texture(
                data,
                int(payload.details["width"]),
                int(payload.details["height"]),
                int(payload.details["texture_format"]),
            )
            from PIL import Image

            image = Image.frombytes("RGBA", (decoded.width, decoded.height), decoded.rgba)
            image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            output = io.BytesIO()
            image.save(output, "PNG")
            return output.getvalue()
        except (OSError, EOFError, TextureDecodeError, ValueError):
            return None

def export_reconstructed_glbs(
    context: ModelContextIndex,
    output_root: Path,
    *,
    embed_taildata: bool,
    reserve_path: ReservePath,
    kinds: set[str] | None = None,
    files: set[str] | None = None,
    limit: int | None = None,
) -> ReconstructedBatch:
    selected_kinds = kinds or {"scene", "prefab", "terrain", "navmesh"}
    batch = ReconstructedBatch()
    source = ReconstructionSource(context)

    def available() -> bool:
        return limit is None or len(batch.results) < limit

    if "scene" in selected_kinds:
        scene_names = read_scene_names(context)
        for asset_key in sorted(context.assets, key=asset_sort_key):
            if not available():
                return batch
            if not is_scene_asset(asset_key) or not asset_selected(context.assets[asset_key], files):
                continue
            try:
                result = export_scene(context, source, output_root, asset_key, scene_names, embed_taildata, reserve_path)
                batch.results.append(result)
            except Exception as exc:
                batch.fail("reconstructed_scene", str(asset_key), exc)

    if "prefab" in selected_kinds:
        roots = root_transforms(context, include_scenes=False)
        for root_key in roots:
            if not available():
                return batch
            asset = context.assets[root_key[0]]
            if not asset_selected(asset, files):
                continue
            try:
                result = export_prefab(context, source, output_root, root_key, embed_taildata, reserve_path)
                batch.results.append(result)
            except Exception as exc:
                batch.fail("reconstructed_prefab", "%s:%s" % root_key, exc)

    for kind, class_id in (("terrain", 156), ("navmesh", 238)):
        if kind not in selected_kinds:
            continue
        for asset_key in sorted(context.assets):
            if not available():
                return batch
            asset = context.assets[asset_key]
            if not asset_selected(asset, files):
                continue
            for obj in asset.objects:
                if not available():
                    return batch
                if obj.class_id != class_id:
                    continue
                try:
                    if kind == "terrain":
                        result = export_terrain(
                            context,
                            output_root,
                            asset,
                            obj,
                            embed_taildata,
                            reserve_path,
                        )
                    else:
                        result = export_navmesh(
                            context,
                            output_root,
                            asset,
                            obj,
                            embed_taildata,
                            reserve_path,
                        )
                    batch.results.append(result)
                except Exception as exc:
                    batch.fail("reconstructed_" + kind, export_log.source_label(asset, obj), exc)
    return batch

def export_scene(
    context: ModelContextIndex,
    source: ReconstructionSource,
    output_root: Path,
    asset_key: str,
    scene_names: dict[str, str],
    embed_taildata: bool,
    reserve_path: ReservePath,
) -> ReconstructedResult:
    asset = context.assets[asset_key]
    name = scene_names.get(asset_key, Path(asset.relative_path).name)
    roots = [key for key in root_transforms(context, include_scenes=True) if key[0] == asset_key]
    selected = {key for key in context.transforms if key[0] == asset_key}
    metadata = hierarchy_metadata(asset, name, "reconstructed_scene_glb", roots)
    path = reserve_path(
        output_root / "converted" / "glb" / "SceneHierarchyObject" / f"{safe_name(name) or 'Scene'}.glb",
        asset.relative_path,
    )
    metrics = write_hierarchy_glb(context, source, selected, roots, name, path, metadata if embed_taildata else None)
    metadata["conversion"] = metrics
    manifest_path, manifest_metadata = write_component_manifest(
        context,
        selected,
        roots,
        name,
        "scene",
        path,
        output_root,
        reserve_path,
        asset.relative_path,
    )
    return ReconstructedResult(path, metadata, "scene", manifest_path, manifest_metadata)

def export_prefab(
    context: ModelContextIndex,
    source: ReconstructionSource,
    output_root: Path,
    root_key: ObjectKey,
    embed_taildata: bool,
    reserve_path: ReservePath,
) -> ReconstructedResult:
    asset = context.assets[root_key[0]]
    root = context.transforms[root_key]
    name = root.name or f"Prefab_{root_key[1]}"
    selected = descendant_transforms(context, root_key)
    metadata = hierarchy_metadata(asset, name, "reconstructed_prefab_glb", [root_key])
    game_object_key = context.resolve(root.asset_key, root.game_object)
    if game_object_key is not None:
        metadata["root_game_object_path_id"] = game_object_key[1]
    stable_id = game_object_key[1] if game_object_key is not None else root_key[1]
    path = reserve_path(
        output_root
        / "converted"
        / "glb"
        / "PrefabHierarchyObject"
        / f"{safe_name(name) or 'Prefab'}__path{stable_id}.glb",
        f"{asset.relative_path}:{stable_id}",
    )
    metrics = write_hierarchy_glb(
        context,
        source,
        selected,
        [root_key],
        name,
        path,
        metadata if embed_taildata else None,
    )
    metadata["conversion"] = metrics
    manifest_path, manifest_metadata = write_component_manifest(
        context,
        selected,
        [root_key],
        name,
        "prefab",
        path,
        output_root,
        reserve_path,
        f"{asset.relative_path}:{stable_id}",
    )
    return ReconstructedResult(path, metadata, "prefab", manifest_path, manifest_metadata)

def write_component_manifest(
    context: ModelContextIndex,
    selected: set[ObjectKey],
    roots: list[ObjectKey],
    name: str,
    kind: str,
    glb_path: Path,
    output_root: Path,
    reserve_path: ReservePath,
    source_key: str,
) -> tuple[Path, dict]:
    ordered = sorted(selected, key=lambda key: (depth_in_set(context, key, selected), key[0], key[1]))
    game_objects: list[dict] = []
    class_counts: dict[str, int] = {}
    component_count = 0
    unresolved_count = 0

    for transform_key in ordered:
        transform = context.transforms[transform_key]
        game_object_key = context.resolve(transform.asset_key, transform.game_object)
        game_object = context.game_objects.get(game_object_key) if game_object_key is not None else None
        parent_key = context.resolve(transform.asset_key, transform.father)
        components: list[dict] = []
        if game_object is not None:
            for pointer in game_object.components:
                target_key = context.resolve(game_object.asset_key, pointer)
                target_obj = context.objects.get(target_key) if target_key is not None else None
                if target_key is None or target_obj is None:
                    unresolved_count += 1
                    components.append(
                        {
                            "fileId": pointer.file_id,
                            "pathId": pointer.path_id,
                            "resolved": False,
                        }
                    )
                    continue
                component_name = class_name(target_obj.class_id)
                class_label = f"{target_obj.class_id:03d}_{component_name}"
                class_counts[class_label] = class_counts.get(class_label, 0) + 1
                component_count += 1
                components.append(
                    {
                        "container": target_key[0],
                        "pathId": target_key[1],
                        "classId": target_obj.class_id,
                        "className": component_name,
                        "editableExportKind": component_editable_kind(target_obj.class_id),
                        "resolved": True,
                    }
                )
        game_objects.append(
            {
                "name": transform.name or (game_object.name if game_object is not None else ""),
                "container": transform_key[0],
                "gameObjectPathId": game_object_key[1] if game_object_key is not None else 0,
                "transformPathId": transform_key[1],
                "transformClassId": context.objects[transform_key].class_id,
                "parentTransformPathId": parent_key[1] if parent_key in selected else 0,
                "layer": game_object.layer if game_object is not None else 0,
                "tag": game_object.tag if game_object is not None else 0,
                "active": game_object.active if game_object is not None else True,
                "components": components,
            }
        )

    relative_glb = relative_output_path(glb_path, output_root)
    document = {
        "format": "sunflowyr-component-manifest",
        "version": 1,
        "game": "rwby-ge",
        "kind": kind,
        "name": name,
        "sourceGlb": relative_glb,
        "roots": [{"container": key[0], "transformPathId": key[1]} for key in roots],
        "gameObjectCount": len(game_objects),
        "componentCount": component_count,
        "unresolvedComponentCount": unresolved_count,
        "componentClassCounts": dict(sorted(class_counts.items())),
        "gameObjects": game_objects,
    }
    manifest_path = reserve_path(
        glb_path.with_suffix(".components.json"),
        f"{source_key}:components",
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    asset = context.assets[roots[0][0]] if roots else context.assets[ordered[0][0]]
    metadata = hierarchy_metadata(
        asset,
        name,
        f"reconstructed_{kind}_component_manifest",
        roots,
    )
    metadata["view_of"] = relative_glb
    metadata["conversion"] = {
        "format": "sunflowyr_component_manifest",
        "gameObjectCount": len(game_objects),
        "componentCount": component_count,
        "unresolvedComponentCount": unresolved_count,
        "componentClassCounts": dict(sorted(class_counts.items())),
    }
    metadata["writeback_note"] = (
        "Component manifest maps reconstructed hierarchy nodes to Unity GameObjects and attached component objects."
    )
    return manifest_path, metadata

def component_editable_kind(class_id: int) -> str | None:
    return {
        4: "transform_json",
        20: "camera_json",
        23: "mesh_renderer_json",
        29: "occlusion_culling_settings_json",
        33: "mesh_filter_json",
        43: "mesh_glb",
        54: "rigidbody_json",
        64: "mesh_collider_json",
        65: "box_collider_json",
        74: "animation_clip_package",
        82: "audio_source_json",
        102: "textmesh_json",
        108: "light_json",
        114: "monobehaviour_json",
        134: "physic_material_json",
        135: "sphere_collider_json",
        136: "capsule_collider_json",
        137: "skinned_mesh_renderer_json",
        143: "character_controller_json",
        154: "terrain_json",
        157: "lightmap_settings_json",
        167: "audio_reverb_zone_json",
        182: "wind_zone_json",
        196: "navmesh_json",
        198: "particle_system_json",
        199: "particle_system_renderer_json",
        205: "lod_group_json",
        215: "reflection_probe_json",
        218: "terrain_json",
        222: "canvas_renderer_json",
        223: "canvas_json",
        224: "rect_transform_json",
        225: "canvas_group_json",
        227: "billboard_renderer_json",
    }.get(class_id)

def relative_output_path(path: Path, output_root: Path) -> str:
    try:
        return path.resolve().relative_to(output_root.resolve()).as_posix()
    except ValueError:
        return path.name

SKELETON_HELPER_SKIN = "Sunflowyr Skeleton Nodes"

def loose_skeleton_nodes(document: dict) -> list[int]:
    nodes = document.get("nodes") or []
    joints = {int(joint) for skin in document.get("skins") or [] for joint in skin.get("joints") or []}
    if not joints:
        return []
    parents = {int(child): index for index, node in enumerate(nodes) for child in node.get("children") or []}
    found = []
    for index in range(len(nodes)):
        if index in joints or "skin" in nodes[index]:
            continue
        at = parents.get(index)
        while at is not None and at not in joints:
            at = parents.get(at)
        if at is not None:
            found.append(index)
    return found

def write_hierarchy_glb(
    context: ModelContextIndex,
    source: ReconstructionSource,
    selected: set[ObjectKey],
    roots: list[ObjectKey],
    name: str,
    path: Path,
    taildata: dict | None,
) -> dict:
    builder = GlbBuilder(name, taildata)
    ordered = sorted(selected, key=lambda key: (depth_in_set(context, key, selected), key[0], key[1]))
    global_matrices = compute_global_matrices(context, ordered, selected)
    node_indices: dict[ObjectKey, int] = {}
    skipped_meshes = 0

    for key in ordered:
        transform = context.transforms[key]
        game_object_key = context.resolve(transform.asset_key, transform.game_object)
        node_name = transform.name or f"GameObject_{game_object_key[1] if game_object_key else key[1]}"
        extras = {
            "unity_container": key[0],
            "unity_transform_path_id": key[1],
            "unity_game_object_path_id": game_object_key[1] if game_object_key is not None else 0,
            "unity_name": node_name,
        }
        node_indices[key] = builder.add_node(
            node_name,
            translation=gltf_position(transform.local_position),
            rotation=gltf_rotation(transform.local_rotation),
            scale=transform.local_scale,
            extras=extras,
        )

    for key in ordered:
        transform = context.transforms[key]
        children = [
            node_indices[child_key]
            for child in transform.children
            if (child_key := context.resolve(transform.asset_key, child)) in node_indices
        ]
        builder.set_node_children(node_indices[key], children)

    builder.set_scene_roots([node_indices[key] for key in roots if key in node_indices])

    for key in ordered:
        transform = context.transforms[key]
        game_object_key = context.resolve(transform.asset_key, transform.game_object)
        if game_object_key is None:
            continue
        mesh_filter = context.mesh_filters_by_game_object.get(game_object_key)
        renderers = context.renderers_by_game_object.get(game_object_key, [])
        renderer = next((item for item in renderers if not item.skinned), None)
        if mesh_filter is None or renderer is None:
            continue
        mesh_key = context.resolve(mesh_filter.asset_key, mesh_filter.mesh)
        if mesh_key is None:
            continue
        try:
            mesh = source.mesh(mesh_key)
        except MeshDecodeError:
            skipped_meshes += 1
            continue
        selected_submeshes = list(renderer.subset_indices) if renderer.subset_indices else list(range(len(mesh.submeshes)))
        materials = [
            context.scene_material(
                context.resolve(renderer.asset_key, renderer.materials[slot]) if slot < len(renderer.materials) else None,
                slot,
            )
            for slot in range(max(1, len(selected_submeshes)))
        ]
        static_batch = bool(renderer.subset_indices)
        position_matrix = matrix_inverse(global_matrices[key]) if static_batch else None
        normal_matrix = matrix_transpose(global_matrices[key]) if static_batch else None
        tangent_matrix = position_matrix if static_batch else None
        mesh_index = builder.add_mesh(
            mesh,
            selected_submeshes,
            materials,
            source.texture_png,
            source_extras={
                "unity_container": mesh_key[0],
                "unity_mesh_path_id": mesh_key[1],
                "unity_renderer_container": renderer.key[0],
                "unity_renderer_path_id": renderer.key[1],
                "static_batch": static_batch,
            },
            position_matrix=position_matrix,
            normal_matrix=normal_matrix,
            tangent_matrix=tangent_matrix,
            cache_key=None,
        )
        if mesh_index is not None:
            builder.document["nodes"][node_indices[key]]["mesh"] = mesh_index
        else:
            skipped_meshes += 1

    for key in ordered:
        transform = context.transforms[key]
        game_object_key = context.resolve(transform.asset_key, transform.game_object)
        if game_object_key is None:
            continue
        for renderer in context.renderers_by_game_object.get(game_object_key, []):
            if not renderer.skinned or renderer.mesh is None:
                continue
            mesh_key = context.resolve(renderer.asset_key, renderer.mesh)
            if mesh_key is None:
                continue
            try:
                mesh = source.mesh(mesh_key)
            except MeshDecodeError:
                skipped_meshes += 1
                continue
            joints = [node_indices.get(context.resolve(renderer.asset_key, bone)) for bone in renderer.bones]
            has_skin = (
                bool(joints)
                and all(joint is not None for joint in joints)
                and len(mesh.bind_poses) == len(joints)
                and len(mesh.skin) == len(mesh.vertices)
            )
            submeshes = list(range(len(mesh.submeshes)))
            materials = [
                context.scene_material(
                    context.resolve(renderer.asset_key, renderer.materials[slot]) if slot < len(renderer.materials) else None,
                    slot,
                )
                for slot in range(max(1, len(submeshes)))
            ]
            mesh_index = builder.add_mesh(
                mesh,
                submeshes,
                materials,
                source.texture_png,
                source_extras={
                    "unity_container": mesh_key[0],
                    "unity_mesh_path_id": mesh_key[1],
                    "unity_renderer_container": renderer.key[0],
                    "unity_renderer_path_id": renderer.key[1],
                    "skinned": True,
                },
                skinned=has_skin,
            )
            if mesh_index is None:
                skipped_meshes += 1
                continue
            node = builder.document["nodes"][node_indices[key]]
            if "mesh" in node:
                child = builder.add_node(f"{node.get('name') or 'Mesh'} Skinned")
                node.setdefault("children", []).append(child)
                node = builder.document["nodes"][child]
            node["mesh"] = mesh_index
            if has_skin:
                matrices = array("f")
                for values in mesh.bind_poses:
                    matrices.extend(gltf_bind_pose(values))
                builder.document["skins"].append(
                    {
                        "name": f"{mesh.name or 'Mesh'} Skin",
                        "joints": joints,
                        "inverseBindMatrices": builder.add_accessor(matrices, GL_FLOAT, len(joints), "MAT4"),
                    }
                )
                node["skin"] = len(builder.document["skins"]) - 1

    skeleton_nodes = loose_skeleton_nodes(builder.document)
    if skeleton_nodes:
        builder.document["skins"].append({"name": SKELETON_HELPER_SKIN, "joints": skeleton_nodes})
    metrics = builder.metrics()
    metrics["skipped_meshes"] = skipped_meshes
    if taildata is not None:
        taildata["conversion"] = metrics
    builder.write(path)
    return metrics

def export_terrain(
    context: ModelContextIndex,
    output_root: Path,
    asset: SerializedAsset,
    obj: SerializedObject,
    embed_taildata: bool,
    reserve_path: ReservePath,
) -> ReconstructedResult:
    terrain = read_terrain_data(context, asset, obj)
    sidecars = write_terrain_sidecars(terrain, output_root, asset, obj, reserve_path)
    metadata = object_metadata(asset, obj, terrain.name, "reconstructed_terrain_glb")
    path = reserve_path(
        output_root
        / "converted"
        / "glb"
        / "156_TerrainData"
        / f"{safe_name(terrain.name) or 'TerrainData'}__path{obj.path_id}.glb",
        f"{asset.relative_path}:{obj.path_id}",
    )
    builder = GlbBuilder(terrain.name, metadata if embed_taildata else None)
    positions, uv0, indices = terrain_arrays(terrain)
    source_extras = {"unity_container": asset.relative_path, "unity_path_id": obj.path_id}
    mesh_index = builder.add_raw_mesh(
        terrain.name,
        positions,
        indices,
        uv0=uv0,
        material=default_material("DefaultMaterial"),
        extras=source_extras,
    )
    node_index = builder.add_node(terrain.name, mesh=mesh_index, extras=source_extras)
    builder.set_scene_roots([node_index])
    metrics = builder.metrics()
    metrics.update(
        {
            "width": terrain.width,
            "height": terrain.height,
            "vertices": terrain.width * terrain.height,
            "triangles": (terrain.width - 1) * (terrain.height - 1) * 2,
            "scale": list(terrain.scale),
        }
    )
    metadata["conversion"] = metrics
    metadata["terrain_writeback_supported"] = True
    metadata["terrain_writeback_format"] = "terrain_glb_heightfield_to_unity_object"
    metadata["writeback_note"] = (
        "Terrain GLB supports heightfield sculpting. Keep every original grid vertex and dont move "
        "vertices horizontally, the AssetBundle generator maps edited Y positions back into TerrainData heights."
    )
    builder.write(path)
    terrain_json_metadata = object_metadata(asset, obj, terrain.name, "terrain_data_json")
    terrain_json_metadata["conversion"] = {
        "format": "sunflowyr_terrain_data",
        **sidecars.metrics,
    }
    terrain_json_metadata["view_of"] = relative_output_path(path, output_root)
    terrain_json_metadata["writeback_note"] = (
        "Terrain JSON stores splat, detail, tree, component, and material-link metadata. "
        "Large editable arrays are little-endian binary companions named in binaryFiles. "
        "Keep the JSON and companions together, the AssetBundle generator rebuilds the Unity TerrainData object."
    )
    terrain_json_metadata["terrain_writeback_supported"] = True
    terrain_json_metadata["terrain_writeback_format"] = "terrain_data_json_to_unity_object"
    auxiliary_files: list[tuple[Path, dict]] = []
    for role, binary_path in sidecars.binary_paths.items():
        binary_metadata = object_metadata(asset, obj, terrain.name, f"terrain_{role}_binary")
        binary_metadata["view_of"] = relative_output_path(sidecars.json_path, output_root)
        binary_metadata["binary_role"] = role
        binary_metadata["conversion"] = {
            "format": "terrain_binary_companion",
            "role": role,
            "byteOrder": "little-endian",
            "byteLength": binary_path.stat().st_size,
        }
        auxiliary_files.append((binary_path, binary_metadata))
    return ReconstructedResult(
        path,
        metadata,
        "terrain",
        terrain_json_path=sidecars.json_path,
        terrain_json_metadata=terrain_json_metadata,
        auxiliary_files=auxiliary_files,
    )

def export_navmesh(
    context: ModelContextIndex,
    output_root: Path,
    asset: SerializedAsset,
    obj: SerializedObject,
    embed_taildata: bool,
    reserve_path: ReservePath,
) -> ReconstructedResult:
    navmesh = read_navmesh_data(context, asset, obj)
    sidecars = write_navmesh_sidecars(navmesh, output_root, asset, obj, reserve_path)
    name = navmesh.name or "NavMesh"
    metadata = object_metadata(asset, obj, name, "reconstructed_navmesh_glb")
    path = reserve_path(
        output_root
        / "converted"
        / "glb"
        / "238_NavMeshData"
        / f"{safe_name(name) or 'NavMesh'}__path{obj.path_id}.glb",
        f"{asset.relative_path}:{obj.path_id}",
    )
    builder = GlbBuilder(name, metadata if embed_taildata else None)
    source_extras = {
        "unity_container": asset.relative_path,
        "unity_path_id": obj.path_id,
        "unity_navmesh_tile_count": sidecars.metrics["tileCount"],
        "unity_navmesh_polygon_count": sidecars.metrics["polygonCount"],
    }
    mesh_index = builder.add_raw_mesh(
        name,
        navmesh.positions,
        navmesh.indices,
        material=default_material("NavMesh Walkable Surface"),
        extras=source_extras,
    )
    node_index = builder.add_node(name, mesh=mesh_index, extras=source_extras)
    builder.set_scene_roots([node_index])
    metrics = builder.metrics()
    metrics.update(sidecars.metrics)
    metadata["conversion"] = metrics
    builder.write(path)
    navmesh_json_metadata = object_metadata(asset, obj, name, "navmesh_data_json")
    navmesh_json_metadata["conversion"] = {
        "format": "sunflowyr_navmesh_data",
        **sidecars.metrics,
    }
    navmesh_json_metadata["view_of"] = relative_output_path(path, output_root)
    navmesh_json_metadata["writeback_note"] = (
        "NavMesh JSON maps every Detour tile and preserves exact tile blobs for write-back. "
        "The GLB is editable geometry but edited geometry must be rebaked into version 16 tiles before repacking."
    )
    navmesh_json_metadata["navmesh_writeback_supported"] = True
    navmesh_json_metadata["navmesh_writeback_format"] = "navmesh_data_json_to_unity_object"
    tile_metadata = object_metadata(asset, obj, name, "navmesh_tile_binary")
    tile_metadata["view_of"] = relative_output_path(sidecars.json_path, output_root)
    tile_metadata["binary_role"] = "navmeshTiles"
    tile_metadata["conversion"] = {
        "format": "detour_navmesh_tile_blobs",
        "version": 16,
        "byteOrder": "little-endian",
        "byteLength": sidecars.tile_blob_path.stat().st_size,
    }
    return ReconstructedResult(
        path,
        metadata,
        "navmesh",
        navmesh_json_path=sidecars.json_path,
        navmesh_json_metadata=navmesh_json_metadata,
        auxiliary_files=[(sidecars.tile_blob_path, tile_metadata)],
    )

def read_scene_names(context: ModelContextIndex) -> dict[str, str]:
    scenes: list[str] = []
    for asset in context.assets.values():
        obj = next((item for item in asset.objects if item.class_id == 141), None)
        if obj is None:
            continue
        try:
            with asset.path.open("rb") as handle:
                reader = EndianReader(handle, asset.endian)
                reader.seek(obj.byte_start)
                count = reader.i32()
                if count < 0 or count > 10000:
                    continue
                scenes = [reader.aligned_string(max_length=4096) for _ in range(count)]
                break
        except (OSError, EOFError, ValueError):
            continue
    output: dict[str, str] = {}
    for index, path in enumerate(scenes):
        output[f"level{index}"] = Path(path.replace("\\", "/")).stem or f"level{index}"
    return output

def terrain_arrays(terrain: TerrainDataRecord) -> tuple[array, array, array]:
    width = terrain.width
    height = terrain.height
    scale_x, scale_y, scale_z = terrain.scale
    uv_x = 1.0 / (width - 1)
    uv_y = 1.0 / (height - 1)
    positions = array("f")
    uv0 = array("f")
    for y in range(height):
        for x in range(width):
            height_value = terrain.heights[x + y * width] / 32767.0
            positions.extend((-(y * scale_x), height_value * scale_y, x * scale_z))
            uv0.extend((x * uv_x, y * uv_y))
    indices = array("I")
    for y in range(height - 1):
        row = y * width
        next_row = row + width
        for x in range(width - 1):
            a = row + x
            b = a + 1
            c = next_row + x
            d = c + 1
            indices.extend((a, c, b, c, d, b))
    return positions, uv0, indices

def root_transforms(context: ModelContextIndex, include_scenes: bool) -> list[ObjectKey]:
    roots: list[ObjectKey] = []
    for key, transform in context.transforms.items():
        is_scene = is_scene_asset(key[0])
        if is_scene != include_scenes:
            continue
        parent = context.resolve(transform.asset_key, transform.father)
        if parent not in context.transforms:
            roots.append(key)
    return sorted(roots, key=lambda key: (key[0], key[1]))

def descendant_transforms(context: ModelContextIndex, root: ObjectKey) -> set[ObjectKey]:
    selected: set[ObjectKey] = set()
    pending = [root]
    while pending:
        key = pending.pop()
        if key in selected:
            continue
        transform = context.transforms.get(key)
        if transform is None:
            continue
        selected.add(key)
        for child in transform.children:
            child_key = context.resolve(transform.asset_key, child)
            if child_key is not None and child_key not in selected:
                pending.append(child_key)
    return selected

def compute_global_matrices(
    context: ModelContextIndex,
    ordered: list[ObjectKey],
    selected: set[ObjectKey],
) -> dict[ObjectKey, Matrix]:
    output: dict[ObjectKey, Matrix] = {}
    for key in ordered:
        transform = context.transforms[key]
        local = unity_trs_matrix(transform)
        parent = context.resolve(transform.asset_key, transform.father)
        output[key] = matrix_multiply(output[parent], local) if parent in selected else local
    return output

def depth_in_set(context: ModelContextIndex, key: ObjectKey, selected: set[ObjectKey]) -> int:
    depth = 0
    current: ObjectKey | None = key
    seen: set[ObjectKey] = set()
    while current is not None and current in selected and current not in seen:
        seen.add(current)
        transform = context.transforms.get(current)
        if transform is None:
            break
        current = context.resolve(transform.asset_key, transform.father)
        depth += 1
    return depth

def unity_trs_matrix(transform: TransformRecord) -> Matrix:
    x, y, z, w = normalize_quaternion(transform.local_rotation)
    sx, sy, sz = transform.local_scale
    px, py, pz = transform.local_position
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    return (
        ((1.0 - 2.0 * (yy + zz)) * sx, (2.0 * (xy - wz)) * sy, (2.0 * (xz + wy)) * sz, px),
        ((2.0 * (xy + wz)) * sx, (1.0 - 2.0 * (xx + zz)) * sy, (2.0 * (yz - wx)) * sz, py),
        ((2.0 * (xz - wy)) * sx, (2.0 * (yz + wx)) * sy, (1.0 - 2.0 * (xx + yy)) * sz, pz),
        (0.0, 0.0, 0.0, 1.0),
    )

def matrix_multiply(left: Matrix, right: Matrix) -> Matrix:
    return tuple(
        tuple(sum(left[row][item] * right[item][column] for item in range(4)) for column in range(4))
        for row in range(4)
    )

def matrix_inverse(matrix: Matrix) -> Matrix:
    identity = (
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )
    work = [list(row) + list(identity_row) for row, identity_row in zip(matrix, identity)]
    for column in range(4):
        pivot = max(range(column, 4), key=lambda row: abs(work[row][column]))
        if abs(work[pivot][column]) < 1e-12:
            raise ValueError("hierarchy transform matrix is singular")
        work[column], work[pivot] = work[pivot], work[column]
        scale = work[column][column]
        work[column] = [value / scale for value in work[column]]
        for row in range(4):
            if row == column:
                continue
            factor = work[row][column]
            work[row] = [value - factor * pivot_value for value, pivot_value in zip(work[row], work[column])]
    return tuple(tuple(row[4:]) for row in work)

def matrix_transpose(matrix: Matrix) -> Matrix:
    return tuple(tuple(matrix[column][row] for column in range(4)) for row in range(4))

def normalize_quaternion(values: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    length = math.sqrt(sum(value * value for value in values))
    if length < 1e-12:
        return 0.0, 0.0, 0.0, 1.0
    return tuple(value / length for value in values)

def gltf_position(position: tuple[float, float, float]) -> tuple[float, float, float]:
    return -position[0], position[1], position[2]

def gltf_rotation(rotation: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    return rotation[0], -rotation[1], -rotation[2], rotation[3]

def hierarchy_metadata(asset: SerializedAsset, name: str, kind: str, roots: list[ObjectKey]) -> dict:
    return {
        "sunflowyrTaildata": 1,
        "kind": kind,
        "unity_version": asset.unity_version,
        "serialized_format_version": asset.format_version,
        "container": asset.relative_path,
        "append_target": asset.relative_path,
        "name": name,
        "root_transform_path_ids": [key[1] for key in roots],
        "writeback_note": (
            "GLB is a reconstructed view/edit format. Node and mesh extras preserve Unity source object identities, "
            "convert edited content back to its source objects before repacking."
        ),
    }

def object_metadata(asset: SerializedAsset, obj: SerializedObject, name: str, kind: str) -> dict:
    return {
        "sunflowyrTaildata": 1,
        "kind": kind,
        "unity_version": asset.unity_version,
        "serialized_format_version": asset.format_version,
        "container": asset.relative_path,
        "append_target": asset.relative_path,
        "path_id": obj.path_id,
        "class_id": obj.class_id,
        "class_name": obj.type_name,
        "name": name,
        "object_entry_offset": obj.entry_offset,
        "byte_start_field_offset": obj.byte_start_field_offset,
        "byte_size_field_offset": obj.byte_size_field_offset,
        "byte_start_relative": obj.byte_start - asset.data_offset,
        "byte_start_absolute": obj.byte_start,
        "byte_size": obj.byte_size,
        "data_offset": asset.data_offset,
        "writeback_note": "GLB is a view/edit format, encode changes back to the source Unity object before repacking.",
    }

def default_material(name: str) -> SceneMaterial:
    return SceneMaterial(
        key=None,
        name=name,
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

def resolve_resource(asset: SerializedAsset, source_path: str) -> Path | None:
    if source_path == asset.relative_path:
        return asset.path
    candidate = asset.path.parent / source_path
    if candidate.exists():
        return candidate
    matches = list(asset.path.parent.rglob(Path(source_path).name))
    return matches[0] if matches else None

def asset_selected(asset: SerializedAsset, files: set[str] | None) -> bool:
    if not files:
        return True
    return asset.relative_path.lower() in files or asset.path.name.lower() in files

def is_scene_asset(asset_key: str) -> bool:
    return re.fullmatch(r"level\d+", Path(asset_key).name) is not None

def asset_sort_key(asset_key: str) -> tuple[int, str]:
    match = re.fullmatch(r"level(\d+)", Path(asset_key).name)
    return (int(match.group(1)), asset_key) if match else (1_000_000, asset_key)

def safe_name(value: str, max_len: int = 120) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:max_len].rstrip(" .")
