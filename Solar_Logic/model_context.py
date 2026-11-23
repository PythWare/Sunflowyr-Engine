from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from . import export_log
from .binary import EndianReader
from .class_ids import NAMED_OBJECT_CLASS_IDS
from .serialized import SerializedAsset, SerializedObject
from .unity_typetree_codec import load_schema, read_typetree

ObjectKey = tuple[str, int]

@dataclass(frozen=True, slots=True)
class PPtr:
    file_id: int
    path_id: int

@dataclass(slots=True)
class GameObjectRecord:
    key: ObjectKey
    asset_key: str
    name: str
    components: list[PPtr]
    layer: int
    tag: int
    active: bool

@dataclass(slots=True)
class AnimationRecord:
    asset_key: str
    game_object: PPtr
    clips: list[PPtr]

@dataclass(slots=True)
class AnimatorRecord:
    asset_key: str
    game_object: PPtr
    controller: PPtr

@dataclass(slots=True)
class AnimatorControllerRecord:
    asset_key: str
    clips: list[PPtr]

@dataclass(slots=True)
class TerrainComponentRecord:
    key: ObjectKey
    asset_key: str
    game_object: PPtr
    enabled: bool
    terrain_data: PPtr
    material_template: PPtr
    settings: dict[str, int | float | bool]

@dataclass(slots=True)
class TerrainColliderRecord:
    key: ObjectKey
    asset_key: str
    game_object: PPtr
    material: PPtr
    enabled: bool
    terrain_data: PPtr
    enable_tree_colliders: bool

@dataclass(slots=True)
class NavMeshSettingsRecord:
    key: ObjectKey
    asset_key: str
    navmesh_data: PPtr

@dataclass(slots=True)
class TransformRecord:
    key: ObjectKey
    asset_key: str
    game_object: PPtr
    name: str
    local_rotation: tuple[float, float, float, float]
    local_position: tuple[float, float, float]
    local_scale: tuple[float, float, float]
    children: list[PPtr]
    father: PPtr

@dataclass(slots=True)
class MeshFilterRecord:
    asset_key: str
    game_object: PPtr
    mesh: PPtr

@dataclass(slots=True)
class RendererRecord:
    key: ObjectKey
    asset_key: str
    game_object: PPtr
    materials: list[PPtr]
    subset_indices: list[int]
    mesh: PPtr | None
    bones: list[PPtr]
    skinned: bool

@dataclass(slots=True)
class MaterialRecord:
    key: ObjectKey
    asset_key: str
    name: str
    shader_keywords: str
    custom_render_queue: int
    colors: dict[str, tuple[float, float, float, float]]
    floats: dict[str, float]
    textures: dict[str, MaterialTextureRecord]

@dataclass(frozen=True, slots=True)
class MaterialTextureRecord:
    texture: PPtr
    scale: tuple[float, float]
    offset: tuple[float, float]

@dataclass(slots=True)
class SceneNode:
    key: ObjectKey
    name: str
    parent_key: ObjectKey | None
    local_rotation: tuple[float, float, float, float]
    local_position: tuple[float, float, float]
    local_scale: tuple[float, float, float]
    is_bone: bool

@dataclass(frozen=True, slots=True)
class SceneTexture:
    key: ObjectKey
    name: str
    scale: tuple[float, float]
    offset: tuple[float, float]

@dataclass(slots=True)
class SceneMaterial:
    key: ObjectKey | None
    name: str
    diffuse: tuple[float, float, float, float]
    ambient: tuple[float, float, float, float]
    emissive: tuple[float, float, float, float]
    specular: tuple[float, float, float, float]
    reflection: tuple[float, float, float, float]
    shininess: float
    transparency: float
    alpha_mode: str
    textures: dict[str, SceneTexture]

@dataclass(slots=True)
class FbxSceneContext:
    mesh_model_key: ObjectKey
    nodes: list[SceneNode]
    materials: list[SceneMaterial]
    bone_keys: list[ObjectKey | None]
    subset_indices: list[int]
    renderer_key: ObjectKey
    skinned: bool

class ModelContextIndex:
    def __init__(self, assets: list[SerializedAsset]) -> None:
        self.assets = {norm(asset.relative_path): asset for asset in assets}
        self.aliases: dict[str, str] = {}
        self.objects: dict[ObjectKey, SerializedObject] = {}
        self.object_names: dict[ObjectKey, str] = {}
        self.game_objects: dict[ObjectKey, GameObjectRecord] = {}
        self.game_object_names: dict[ObjectKey, str] = {}
        self.animation_records: list[AnimationRecord] = []
        self.animator_records: list[AnimatorRecord] = []
        self.animator_controllers: dict[ObjectKey, AnimatorControllerRecord] = {}
        self.animation_roots_by_clip: dict[ObjectKey, list[ObjectKey]] = {}
        self.terrain_components: list[TerrainComponentRecord] = []
        self.terrain_components_by_data: dict[ObjectKey, list[TerrainComponentRecord]] = {}
        self.terrain_colliders: list[TerrainColliderRecord] = []
        self.terrain_colliders_by_data: dict[ObjectKey, list[TerrainColliderRecord]] = {}
        self.navmesh_settings: list[NavMeshSettingsRecord] = []
        self.navmesh_settings_by_data: dict[ObjectKey, list[NavMeshSettingsRecord]] = {}
        self.transforms: dict[ObjectKey, TransformRecord] = {}
        self.transforms_by_game_object: dict[ObjectKey, ObjectKey] = {}
        self.mesh_filters_by_game_object: dict[ObjectKey, MeshFilterRecord] = {}
        self.renderers: list[RendererRecord] = []
        self.renderers_by_game_object: dict[ObjectKey, list[RendererRecord]] = {}
        self.mesh_bindings: dict[ObjectKey, list[RendererRecord]] = {}
        self.materials: dict[ObjectKey, MaterialRecord] = {}
        self.texture_names: dict[ObjectKey, str] = {}
        self.parse_errors = 0

        for asset_key, asset in self.assets.items():
            self.aliases.setdefault(asset_key, asset_key)
            self.aliases.setdefault(norm(asset.path.name), asset_key)
            for obj in asset.objects:
                self.objects[(asset_key, obj.path_id)] = obj

        self.read_assets()
        self.link_records()

    def key_for(self, asset: SerializedAsset, path_id: int) -> ObjectKey:
        return (norm(asset.relative_path), path_id)

    def resolve(self, asset_key: str, pointer: PPtr | None) -> ObjectKey | None:
        if pointer is None or pointer.file_id < 0 or pointer.path_id == 0:
            return None
        if pointer.file_id == 0:
            return (asset_key, pointer.path_id)
        asset = self.assets.get(asset_key)
        external_index = pointer.file_id - 1
        if asset is None or not (0 <= external_index < len(asset.externals)):
            return None
        external = asset.externals[external_index]
        target_asset_key = self.aliases.get(norm(external)) or self.aliases.get(norm(Path(external).name))
        if target_asset_key is None:
            return None
        return (target_asset_key, pointer.path_id)

    def scene_for_mesh(self, asset: SerializedAsset, obj: SerializedObject) -> FbxSceneContext | None:
        mesh_key = self.key_for(asset, obj.path_id)
        bindings = self.mesh_bindings.get(mesh_key)
        if not bindings:
            return None
        binding = min(
            bindings,
            key=lambda item: (
                0 if item.skinned else 1,
                0 if item.materials else 1,
                item.asset_key,
                item.key[1],
            ),
        )
        game_object_key = self.resolve(binding.asset_key, binding.game_object)
        mesh_model_key = self.transforms_by_game_object.get(game_object_key) if game_object_key else None
        if mesh_model_key is None:
            return None

        bone_keys = [self.resolve(binding.asset_key, bone) for bone in binding.bones]
        included: set[ObjectKey] = set()
        self.add_lineage(mesh_model_key, included)
        for bone_key in bone_keys:
            if bone_key is not None:
                self.add_lineage(bone_key, included)

        bone_key_set = {key for key in bone_keys if key is not None}
        ordered_keys = sorted(included, key=self.transform_depth)
        nodes: list[SceneNode] = []
        for key in ordered_keys:
            transform = self.transforms.get(key)
            if transform is None:
                continue
            parent_key = self.resolve(transform.asset_key, transform.father)
            if parent_key not in included:
                parent_key = None
            nodes.append(
                SceneNode(
                    key=key,
                    name=transform.name or f"Transform_{key[1]}",
                    parent_key=parent_key,
                    local_rotation=transform.local_rotation,
                    local_position=transform.local_position,
                    local_scale=transform.local_scale,
                    is_bone=key in bone_key_set,
                )
            )

        materials = [
            self.build_scene_material(self.resolve(binding.asset_key, pointer), index)
            for index, pointer in enumerate(binding.materials)
        ]
        return FbxSceneContext(
            mesh_model_key=mesh_model_key,
            nodes=nodes,
            materials=materials,
            bone_keys=bone_keys,
            subset_indices=binding.subset_indices,
            renderer_key=binding.key,
            skinned=binding.skinned,
        )

    def scene_material(self, key: ObjectKey | None, index: int = 0) -> SceneMaterial:
        return self.build_scene_material(key, index)

    def read_assets(self) -> None:
        for asset_key, asset in self.assets.items():
            objects_by_class: dict[int, list[SerializedObject]] = {}
            for obj in asset.objects:
                objects_by_class.setdefault(obj.class_id, []).append(obj)
            with asset.path.open("rb") as handle:
                reader = EndianReader(handle, asset.endian)
                for class_id in NAMED_OBJECT_CLASS_IDS:
                    self.read_class(
                        asset_key,
                        asset,
                        reader,
                        objects_by_class.get(class_id, []),
                        self.read_named_name,
                    )
                self.read_class(asset_key, asset, reader, objects_by_class.get(1, []), self.read_game_object)
                self.read_class(asset_key, asset, reader, objects_by_class.get(28, []), self.read_texture_name)
                self.read_class(asset_key, asset, reader, objects_by_class.get(4, []), self.read_transform)
                self.read_class(asset_key, asset, reader, objects_by_class.get(224, []), self.read_transform)
                self.read_class(asset_key, asset, reader, objects_by_class.get(111, []), self.read_animation)
                self.read_class(asset_key, asset, reader, objects_by_class.get(95, []), self.read_animator)
                self.read_class(
                    asset_key,
                    asset,
                    reader,
                    objects_by_class.get(91, []),
                    self.read_animator_controller,
                )
                self.read_class(asset_key, asset, reader, objects_by_class.get(154, []), self.read_terrain_collider)
                self.read_class(asset_key, asset, reader, objects_by_class.get(196, []), self.read_navmesh_settings)
                self.read_class(asset_key, asset, reader, objects_by_class.get(218, []), self.read_terrain_component)
                self.read_class(asset_key, asset, reader, objects_by_class.get(33, []), self.read_mesh_filter)
                self.read_class(asset_key, asset, reader, objects_by_class.get(23, []), self.read_mesh_renderer)
                self.read_class(asset_key, asset, reader, objects_by_class.get(137, []), self.read_skinned_mesh_renderer)
                self.read_class(asset_key, asset, reader, objects_by_class.get(21, []), self.read_material)

    def read_class(self, asset_key, asset, reader, objects, parser) -> None:
        for obj in objects:
            try:
                parser(asset_key, asset, reader, obj)
                if reader.tell() > obj.byte_start + obj.byte_size:
                    raise ValueError("object parser crossed object boundary")
            except Exception as exc:
                self.parse_errors += 1
                export_log.record(
                    "model_context/" + parser.__name__, export_log.source_label(asset, obj), exc
                )

    def read_named_name(self, asset_key, asset, reader, obj) -> None:
        reader.seek(obj.byte_start)
        skip_editor_extension(reader, asset)
        self.object_names[(asset_key, obj.path_id)] = reader.aligned_string(max_length=4096)

    def read_game_object(self, asset_key, asset, reader, obj) -> None:
        reader.seek(obj.byte_start)
        skip_editor_extension(reader, asset)
        version = unity_version_tuple(asset.unity_version)
        component_count = read_count(reader, 1_000_000)
        components: list[PPtr] = []
        for _ in range(component_count):
            if (version[0] == 5 and version[1] < 5) or version[0] < 5:
                reader.i32()
            components.append(read_pptr(reader, asset))
        layer = reader.i32()
        name = reader.aligned_string(max_length=4096)
        tag = reader.u16()
        active = reader.bool()
        key = (asset_key, obj.path_id)
        self.game_objects[key] = GameObjectRecord(key, asset_key, name, components, layer, tag, active)
        self.game_object_names[key] = name

    def read_texture_name(self, asset_key, asset, reader, obj) -> None:
        reader.seek(obj.byte_start)
        skip_editor_extension(reader, asset)
        self.texture_names[(asset_key, obj.path_id)] = reader.aligned_string(max_length=4096)

    def read_transform(self, asset_key, asset, reader, obj) -> None:
        reader.seek(obj.byte_start)
        skip_editor_extension(reader, asset)
        game_object = read_pptr(reader, asset)
        local_rotation = read_vector(reader, 4)
        local_position = read_vector(reader, 3)
        local_scale = read_vector(reader, 3)
        children = [read_pptr(reader, asset) for _ in range(read_count(reader, 1_000_000))]
        father = read_pptr(reader, asset)
        key = (asset_key, obj.path_id)
        game_object_key = self.resolve(asset_key, game_object)
        name = self.game_object_names.get(game_object_key, "") if game_object_key else ""
        self.transforms[key] = TransformRecord(
            key,
            asset_key,
            game_object,
            name,
            local_rotation,
            local_position,
            local_scale,
            children,
            father,
        )
        if game_object_key is not None:
            self.transforms_by_game_object[game_object_key] = key

    def read_mesh_filter(self, asset_key, asset, reader, obj) -> None:
        reader.seek(obj.byte_start)
        skip_editor_extension(reader, asset)
        game_object = read_pptr(reader, asset)
        mesh = read_pptr(reader, asset)
        game_object_key = self.resolve(asset_key, game_object)
        if game_object_key is not None:
            self.mesh_filters_by_game_object[game_object_key] = MeshFilterRecord(asset_key, game_object, mesh)

    def read_animation(self, asset_key, asset, reader, obj) -> None:
        reader.seek(obj.byte_start)
        skip_editor_extension(reader, asset)
        game_object = read_pptr(reader, asset)
        reader.bool()
        reader.align()
        default_clip = read_pptr(reader, asset)
        clips = [read_pptr(reader, asset) for _ in range(read_count(reader, 1_000_000))]
        combined: list[PPtr] = []
        seen: set[tuple[int, int]] = set()
        for pointer in [default_clip, *clips]:
            key = (pointer.file_id, pointer.path_id)
            if pointer.path_id and key not in seen:
                seen.add(key)
                combined.append(pointer)
        self.animation_records.append(AnimationRecord(asset_key, game_object, combined))

    def read_animator(self, asset_key, asset, reader, obj) -> None:
        reader.seek(obj.byte_start)
        skip_editor_extension(reader, asset)
        game_object = read_pptr(reader, asset)
        reader.bool()
        reader.align()
        read_pptr(reader, asset)
        controller = read_pptr(reader, asset)
        self.animator_records.append(AnimatorRecord(asset_key, game_object, controller))

    def read_animator_controller(self, asset_key, asset, reader, obj) -> None:
        reader.seek(obj.byte_start)
        data = reader.read(obj.byte_size)
        fields = read_typetree(data, load_schema("animator_controller_schema.json"), asset.endian)
        clips = [pointer_from_fields(value) for value in fields.get("m_AnimationClips") or []]
        self.animator_controllers[(asset_key, obj.path_id)] = AnimatorControllerRecord(asset_key, clips)

    def read_terrain_component(self, asset_key, asset, reader, obj) -> None:
        reader.seek(obj.byte_start)
        skip_editor_extension(reader, asset)
        game_object = read_pptr(reader, asset)
        enabled = reader.bool()
        reader.align()
        terrain_data = read_pptr(reader, asset)
        settings: dict[str, int | float | bool] = {
            "treeDistance": reader.f32(),
            "treeBillboardDistance": reader.f32(),
            "treeCrossFadeLength": reader.f32(),
            "treeMaximumFullLodCount": reader.i32(),
            "detailObjectDistance": reader.f32(),
            "detailObjectDensity": reader.f32(),
            "heightmapPixelError": reader.f32(),
            "splatMapDistance": reader.f32(),
            "heightmapMaximumLod": reader.i32(),
            "castShadows": reader.bool(),
            "drawHeightmap": reader.bool(),
            "drawTreesAndFoliage": reader.bool(),
        }
        reader.align()
        settings["reflectionProbeUsage"] = reader.i32()
        settings["materialType"] = reader.i32()
        settings["legacySpecularRgba"] = reader.u32()
        settings["legacyShininess"] = reader.f32()
        material_template = read_pptr(reader, asset)
        self.terrain_components.append(
            TerrainComponentRecord(
                key=(asset_key, obj.path_id),
                asset_key=asset_key,
                game_object=game_object,
                enabled=enabled,
                terrain_data=terrain_data,
                material_template=material_template,
                settings=settings,
            )
        )

    def read_terrain_collider(self, asset_key, asset, reader, obj) -> None:
        reader.seek(obj.byte_start)
        skip_editor_extension(reader, asset)
        game_object = read_pptr(reader, asset)
        material = read_pptr(reader, asset)
        enabled = reader.bool()
        reader.align()
        terrain_data = read_pptr(reader, asset)
        enable_tree_colliders = reader.bool()
        self.terrain_colliders.append(
            TerrainColliderRecord(
                key=(asset_key, obj.path_id),
                asset_key=asset_key,
                game_object=game_object,
                material=material,
                enabled=enabled,
                terrain_data=terrain_data,
                enable_tree_colliders=enable_tree_colliders,
            )
        )

    def read_navmesh_settings(self, asset_key, asset, reader, obj) -> None:
        reader.seek(obj.byte_start)
        navmesh_data = read_pptr(reader, asset)
        self.navmesh_settings.append(
            NavMeshSettingsRecord(
                key=(asset_key, obj.path_id),
                asset_key=asset_key,
                navmesh_data=navmesh_data,
            )
        )

    def read_mesh_renderer(self, asset_key, asset, reader, obj) -> None:
        self.renderers.append(self.read_renderer(asset_key, asset, reader, obj, skinned=False))

    def read_skinned_mesh_renderer(self, asset_key, asset, reader, obj) -> None:
        record = self.read_renderer(asset_key, asset, reader, obj, skinned=True)
        reader.i32()
        reader.bool()
        reader.bool()
        reader.align()
        record.mesh = read_pptr(reader, asset)
        record.bones = [read_pptr(reader, asset) for _ in range(read_count(reader, 1_000_000))]
        skip_float_array(reader)
        self.renderers.append(record)

    def read_renderer(self, asset_key, asset, reader, obj, skinned: bool) -> RendererRecord:
        reader.seek(obj.byte_start)
        skip_editor_extension(reader, asset)
        game_object = read_pptr(reader, asset)
        version = unity_version_tuple(asset.unity_version)
        if version[0] != 5 or version[1] < 4:
            raise ValueError("model context currently targets Unity 5.4+ renderer records")
        reader.bool()
        reader.u8()
        reader.u8()
        reader.u8()
        reader.u8()
        reader.u8()
        reader.align()
        reader.u16()
        reader.u16()
        reader.seek(32, 1)
        materials = [read_pptr(reader, asset) for _ in range(read_count(reader, 1_000_000))]
        subset_indices = [reader.u32() for _ in range(read_count(reader, 1_000_000))]
        read_pptr(reader, asset)
        read_pptr(reader, asset)
        read_pptr(reader, asset)
        reader.u32()
        reader.i16()
        reader.align()
        return RendererRecord(
            key=(asset_key, obj.path_id),
            asset_key=asset_key,
            game_object=game_object,
            materials=materials,
            subset_indices=subset_indices,
            mesh=None,
            bones=[],
            skinned=skinned,
        )

    def read_material(self, asset_key, asset, reader, obj) -> None:
        reader.seek(obj.byte_start)
        skip_editor_extension(reader, asset)
        name = reader.aligned_string(max_length=4096)
        read_pptr(reader, asset)
        shader_keywords = reader.aligned_string(max_length=1_000_000)
        reader.u32()
        custom_render_queue = reader.i32()
        for _ in range(read_count(reader, 1_000_000)):
            reader.aligned_string(max_length=4096)
            reader.aligned_string(max_length=4096)

        textures: dict[str, MaterialTextureRecord] = {}
        for _ in range(read_count(reader, 1_000_000)):
            property_name = reader.aligned_string(max_length=4096)
            texture = read_pptr(reader, asset)
            scale = read_vector(reader, 2)
            offset = read_vector(reader, 2)
            textures[property_name] = MaterialTextureRecord(texture, scale, offset)

        floats: dict[str, float] = {}
        for _ in range(read_count(reader, 1_000_000)):
            property_name = reader.aligned_string(max_length=4096)
            floats[property_name] = reader.f32()

        colors: dict[str, tuple[float, float, float, float]] = {}
        for _ in range(read_count(reader, 1_000_000)):
            property_name = reader.aligned_string(max_length=4096)
            colors[property_name] = read_vector(reader, 4)

        key = (asset_key, obj.path_id)
        self.materials[key] = MaterialRecord(
            key,
            asset_key,
            name,
            shader_keywords,
            custom_render_queue,
            colors,
            floats,
            textures,
        )

    def link_records(self) -> None:
        for renderer in self.renderers:
            game_object_key = self.resolve(renderer.asset_key, renderer.game_object)
            if game_object_key is not None:
                self.renderers_by_game_object.setdefault(game_object_key, []).append(renderer)
            if renderer.mesh is None and game_object_key is not None:
                mesh_filter = self.mesh_filters_by_game_object.get(game_object_key)
                if mesh_filter is not None:
                    renderer.mesh = mesh_filter.mesh
            mesh_key = self.resolve(renderer.asset_key, renderer.mesh)
            if mesh_key is not None:
                self.mesh_bindings.setdefault(mesh_key, []).append(renderer)
        for terrain in self.terrain_components:
            terrain_key = self.resolve(terrain.asset_key, terrain.terrain_data)
            if terrain_key is not None:
                self.terrain_components_by_data.setdefault(terrain_key, []).append(terrain)
        for collider in self.terrain_colliders:
            terrain_key = self.resolve(collider.asset_key, collider.terrain_data)
            if terrain_key is not None:
                self.terrain_colliders_by_data.setdefault(terrain_key, []).append(collider)
        for settings in self.navmesh_settings:
            navmesh_key = self.resolve(settings.asset_key, settings.navmesh_data)
            if navmesh_key is not None:
                self.navmesh_settings_by_data.setdefault(navmesh_key, []).append(settings)
        for animation in self.animation_records:
            game_object_key = self.resolve(animation.asset_key, animation.game_object)
            root_key = self.transforms_by_game_object.get(game_object_key) if game_object_key is not None else None
            if root_key is None:
                continue
            for clip in animation.clips:
                clip_key = self.resolve(animation.asset_key, clip)
                if clip_key is None or clip_key not in self.objects:
                    continue
                roots = self.animation_roots_by_clip.setdefault(clip_key, [])
                if root_key not in roots:
                    roots.append(root_key)
        for animator in self.animator_records:
            game_object_key = self.resolve(animator.asset_key, animator.game_object)
            root_key = self.transforms_by_game_object.get(game_object_key) if game_object_key is not None else None
            controller_key = self.resolve(animator.asset_key, animator.controller)
            controller = self.animator_controllers.get(controller_key) if controller_key is not None else None
            if root_key is None or controller is None:
                continue
            for clip in controller.clips:
                clip_key = self.resolve(controller.asset_key, clip)
                if clip_key is None or clip_key not in self.objects:
                    continue
                roots = self.animation_roots_by_clip.setdefault(clip_key, [])
                if root_key not in roots:
                    roots.append(root_key)

    def animation_roots(self, asset: SerializedAsset, obj: SerializedObject) -> list[ObjectKey]:
        return list(self.animation_roots_by_clip.get(self.key_for(asset, obj.path_id), []))

    def animation_model_candidates(self, asset: SerializedAsset, obj: SerializedObject) -> list[ObjectKey]:
        output: list[ObjectKey] = []
        seen_meshes: set[ObjectKey] = set()
        for root_key in self.animation_roots(asset, obj):
            pending = [root_key]
            seen_transforms: set[ObjectKey] = set()
            while pending:
                transform_key = pending.pop()
                if transform_key in seen_transforms:
                    continue
                seen_transforms.add(transform_key)
                transform = self.transforms.get(transform_key)
                if transform is None:
                    continue
                game_object_key = self.resolve(transform.asset_key, transform.game_object)
                renderers = self.renderers_by_game_object.get(game_object_key, [])
                for renderer in renderers:
                    if not renderer.skinned or renderer.mesh is None:
                        continue
                    mesh_key = self.resolve(renderer.asset_key, renderer.mesh)
                    if mesh_key is None or mesh_key in seen_meshes:
                        continue
                    target = self.objects.get(mesh_key)
                    if target is None or target.class_id != 43:
                        continue
                    seen_meshes.add(mesh_key)
                    output.append(mesh_key)
                pending.extend(
                    child_key
                    for pointer in transform.children
                    if (child_key := self.resolve(transform.asset_key, pointer)) is not None
                )
        return output
    def add_lineage(self, key: ObjectKey, included: set[ObjectKey]) -> None:
        current: ObjectKey | None = key
        while current is not None and current not in included:
            transform = self.transforms.get(current)
            if transform is None:
                break
            included.add(current)
            current = self.resolve(transform.asset_key, transform.father)

    def transform_depth(self, key: ObjectKey) -> int:
        depth = 0
        seen: set[ObjectKey] = set()
        current: ObjectKey | None = key
        while current is not None and current not in seen:
            seen.add(current)
            transform = self.transforms.get(current)
            if transform is None:
                break
            current = self.resolve(transform.asset_key, transform.father)
            depth += 1
        return depth

    def build_scene_material(self, key: ObjectKey | None, index: int) -> SceneMaterial:
        material = self.materials.get(key) if key is not None else None
        if material is None:
            return SceneMaterial(
                key=None,
                name="DefaultMaterial",
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
        texture_names: dict[str, SceneTexture] = {}
        for property_name, texture_record in material.textures.items():
            texture_key = self.resolve(material.asset_key, texture_record.texture)
            if texture_key in self.texture_names:
                texture_names[property_name] = SceneTexture(
                    texture_key,
                    self.texture_names[texture_key],
                    texture_record.scale,
                    texture_record.offset,
                )
        return SceneMaterial(
            key=material.key,
            name=material.name or f"Material_{material.key[1]}",
            diffuse=material.colors.get("_Color", (0.8, 0.8, 0.8, 1.0)),
            ambient=material.colors.get("_SColor", (0.2, 0.2, 0.2, 1.0)),
            emissive=material.colors.get("_EmissionColor", (0.0, 0.0, 0.0, 1.0)),
            specular=material.colors.get("_SpecularColor", (0.2, 0.2, 0.2, 1.0)),
            reflection=material.colors.get("_ReflectColor", (0.0, 0.0, 0.0, 1.0)),
            shininess=material.floats.get("_Shininess", 20.0),
            transparency=material.floats.get("_Transparency", 0.0),
            alpha_mode=material_alpha_mode(material),
            textures=texture_names,
        )

def pointer_from_fields(value) -> PPtr:
    fields = value if isinstance(value, dict) else {}
    return PPtr(
        int(fields.get("m_FileID", fields.get("fileId", 0)) or 0),
        int(fields.get("m_PathID", fields.get("pathId", 0)) or 0),
    )

def build_model_context(assets: list[SerializedAsset]) -> ModelContextIndex:
    return ModelContextIndex(assets)

def material_alpha_mode(material: MaterialRecord) -> str:
    keywords = {item.upper() for item in material.shader_keywords.split()}
    mode = material.floats.get("_Mode")
    if "_ALPHATEST_ON" in keywords or mode == 1.0:
        return "MASK"
    if (
        "_ALPHABLEND_ON" in keywords
        or "_ALPHAPREMULTIPLY_ON" in keywords
        or mode in {2.0, 3.0}
        or material.floats.get("_Transparency", 0.0) > 0.001
    ):
        return "BLEND"
    return "OPAQUE"

def read_pptr(reader: EndianReader, asset: SerializedAsset) -> PPtr:
    file_id = reader.i32()
    path_id = reader.i64() if asset.format_version >= 14 else reader.i32()
    return PPtr(file_id, path_id)

def skip_editor_extension(reader: EndianReader, asset: SerializedAsset) -> None:
    if asset.platform == -2:
        reader.u32()
        read_pptr(reader, asset)
        read_pptr(reader, asset)

def read_vector(reader: EndianReader, length: int):
    return tuple(reader.f32() for _ in range(length))

def skip_float_array(reader: EndianReader) -> None:
    reader.seek(read_count(reader, 100_000_000) * 4, 1)

def read_count(reader: EndianReader, maximum: int) -> int:
    count = reader.i32()
    if count < 0 or count > maximum:
        raise ValueError(f"invalid serialized array count {count}")
    return count

def norm(path: str) -> str:
    return path.replace("\\", "/").lower()

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
