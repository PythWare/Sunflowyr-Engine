from __future__ import annotations
import hashlib, json, struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from .asset_bundle_writer import (
    GAME_OBJECT_CLASS_ID,
    MESH_FILTER_CLASS_ID,
    MESH_RENDERER_CLASS_ID,
    MONO_BEHAVIOUR_CLASS_ID,
    MONO_SCRIPT_CLASS_ID,
    RECT_TRANSFORM_CLASS_ID,
    SKINNED_MESH_RENDERER_CLASS_ID,
    TRANSFORM_CLASS_ID,
    ScriptTypeRef,
)
from .class_ids import CLASS_ID_NAMES, COMPONENT_CLASS_IDS
from .editable_object_export import EDITABLE_ASSET_SCHEMAS, EDITABLE_COMPONENT_CLASS_IDS
from .map_component_repack import (
    build_mesh_filter,
    build_mesh_renderer,
    build_skinned_mesh_renderer,
    build_transform,
)
from .managed_schema import UNITY_SERIALIZABLE_TYPES, UNITY_VALUE_TYPES
from .mono_repack import EndianWriter, write_managed_value
from .serialized import SerializedAsset
from .unity_typetree_codec import load_schema, write_typetree

PREFAB_FORMAT = "sunflowyr-new-prefab"
MONO_ASSET_FORMAT = "sunflowyr-new-monobehaviour"
SENTINEL_BASE = 0x4E55_4C46_0000_0000
SENTINEL_LIMIT = SENTINEL_BASE + 0xFFFF_FFFF
DEFAULT_ASSEMBLY = "Assembly-CSharp"

class NewPrefabAssetError(ValueError):
    pass

@dataclass(slots=True)
class BuiltObject:
    class_id: int
    class_name: str
    name: str
    payload: bytes
    local_id: int
    script: ScriptTypeRef | None = None
    container_listed: bool = False

@dataclass(slots=True)
class PrefabBuild:
    objects: list[BuiltObject]
    root_name: str
    details: dict[str, Any]
    external_refs: dict[int, str] = field(default_factory=dict)

class SentinelAllocator:

    def __init__(self, start: int = 0) -> None:
        self.next = start

    def take(self) -> int:
        value = SENTINEL_BASE + self.next
        self.next += 1
        if value > SENTINEL_LIMIT:
            raise NewPrefabAssetError("too many references in one bundle")
        return value

    @property
    def used(self) -> int:
        return self.next

def bundle_stub_asset() -> SerializedAsset:
    return SerializedAsset(
        path=Path("bundle"),
        data_root=Path("."),
        metadata_size=0,
        file_size=0,
        format_version=15,
        data_offset=0,
        endian="<",
        unity_version="5.4.1p4",
        platform=5,
        enable_type_tree=False,
        types=[],
        objects=[],
        externals=[],
        user_information="",
    )

def try_read_prefab_document(json_path: Path) -> dict[str, Any] | None:
    return read_marked_document(json_path, PREFAB_FORMAT, ".prefab.json", "Prefab")

def try_read_mono_document(json_path: Path) -> dict[str, Any] | None:
    return read_marked_document(json_path, MONO_ASSET_FORMAT, ".mono.json", "MonoBehaviour")

def read_marked_document(
    json_path: Path,
    marker: str,
    forced_suffix: str,
    label: str,
) -> dict[str, Any] | None:
    forced = json_path.name.lower().endswith(forced_suffix)
    try:
        document = json.loads(Path(json_path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        if forced:
            raise NewPrefabAssetError(f"couldnt read {label} JSON: {exc}") from exc
        return None
    if not isinstance(document, dict):
        if forced:
            raise NewPrefabAssetError(f"{label} JSON root must be an object")
        return None
    if document.get("format") != marker:
        if forced:
            raise NewPrefabAssetError(f'{label} JSON must set "format": "{marker}"')
        return None
    return document

@dataclass(slots=True)
class NodeSlot:
    game_object_id: int
    transform_id: int
    component_ids: list[int]
    by_type: dict[str, int]

def plan_nodes(
    node: dict[str, Any],
    path: str,
    sentinels: SentinelAllocator,
    plan: dict[str, NodeSlot],
) -> None:
    if not isinstance(node, dict):
        raise NewPrefabAssetError("prefab node must be an object")
    name = str(node.get("name") or "GameObject")
    key = str(node.get("pathName") or name)
    node_path = f"{path}/{key}" if path else key
    slot = NodeSlot(sentinels.take(), sentinels.take(), [], {})
    specs = node.get("components") or []
    if not isinstance(specs, list):
        raise NewPrefabAssetError(f'node "{name}" components must be a list')
    for spec in specs:
        component_id = sentinels.take()
        slot.component_ids.append(component_id)
        if isinstance(spec, dict):
            kind = str(spec.get("type") or "")
            if kind:
                slot.by_type.setdefault(kind, component_id)
            if kind == "MonoBehaviour":
                script = spec.get("script")
                if isinstance(script, dict):
                    class_text = str(script.get("class") or script.get("className") or "")
                    if class_text:
                        slot.by_type.setdefault(class_text, component_id)
    plan.setdefault(node_path, slot)
    for child in node.get("children") or []:
        plan_nodes(child, node_path, sentinels, plan)

NODE_REFERENCE_KEYS = {"prefab", "node", "component", "gameObject"}

def is_prefab_reference(value: dict[str, Any]) -> bool:
    return isinstance(value.get("prefab"), str) and bool(value["prefab"]) and set(value) <= NODE_REFERENCE_KEYS

def resolve_node_reference(
    value: dict[str, Any],
    plan: dict[str, NodeSlot],
    bundle_plans: dict[str, Any] | None = None,
) -> int:
    prefab_file = value.get("prefab")
    if prefab_file:
        if not bundle_plans:
            raise NewPrefabAssetError(
                f'reference to prefab "{prefab_file}" needs that prefab staged in this bundle'
            )
        other = bundle_plans.get(str(prefab_file).casefold())
        if other is None:
            raise NewPrefabAssetError(
                f'reference names prefab "{prefab_file}", which isnt staged in this bundle'
            )
        plan = other
    target = str(value.get("node") or "")
    slot = plan.get(target) if target else next(iter(plan.values()), None)
    if slot is None:
        raise NewPrefabAssetError(
            f'node reference "{target}" doesnt match any node in this prefab'
        )
    component = value.get("component")
    if component:
        if str(component) in {"Transform", "RectTransform"}:
            return slot.transform_id
        found = slot.by_type.get(str(component))
        if found is None:
            raise NewPrefabAssetError(
                f'node "{target}" has no {component} component to reference'
            )
        return found
    if value.get("gameObject"):
        return slot.game_object_id
    return slot.transform_id

def plan_prefab_nodes(
    document: dict[str, Any],
    sentinels: SentinelAllocator,
) -> dict[str, NodeSlot]:
    root_node = document.get("root")
    if not isinstance(root_node, dict):
        raise NewPrefabAssetError('Prefab JSON needs a "root" object')
    plan: dict[str, NodeSlot] = {}
    plan_nodes(root_node, "", sentinels, plan)
    return plan

def build_prefab_objects(
    document: dict[str, Any],
    fallback_name: str,
    sentinels: SentinelAllocator,
    script_pool: dict[tuple[str, str, str], int],
    registry: Any | None,
    plan: dict[str, Any] | None = None,
    bundle_plans: dict[str, Any] | None = None,
) -> PrefabBuild:
    root_node = document.get("root")
    if not isinstance(root_node, dict):
        raise NewPrefabAssetError('Prefab JSON needs a "root" object')
    asset = bundle_stub_asset()
    objects: list[BuiltObject] = []
    external_refs: dict[int, str] = {}
    counts = {"gameObjects": 0, "components": 0, "monoBehaviours": 0}
    if plan is None:
        plan = {}
        plan_nodes(root_node, "", sentinels, plan)

    build_node(
        root_node,
        None,
        objects,
        sentinels,
        script_pool,
        registry,
        asset,
        external_refs,
        counts,
        plan=plan,
        node_path="",
        bundle_plans=bundle_plans,
    )
    if not objects:
        raise NewPrefabAssetError("Prefab JSON produced no objects")
    objects[0].container_listed = True
    name = str(document.get("name") or root_node.get("name") or fallback_name)
    details = {
        "rootGameObject": str(root_node.get("name") or name),
        "objectCount": len(objects),
        **counts,
    }
    return PrefabBuild(objects=objects, root_name=name, details=details, external_refs=external_refs)

def build_node(
    node: dict[str, Any],
    parent_transform_id: int | None,
    objects: list[BuiltObject],
    sentinels: SentinelAllocator,
    script_pool: dict[tuple[str, str, str], int],
    registry: Any | None,
    asset: SerializedAsset,
    external_refs: dict[int, str],
    counts: dict[str, int],
    *,
    inherited_skeleton: Any | None = None,
    plan: dict[str, Any] | None = None,
    node_path: str = "",
    bundle_plans: dict[str, Any] | None = None,
) -> int:
    if not isinstance(node, dict):
        raise NewPrefabAssetError("prefab node must be an object")
    name = str(node.get("name") or "GameObject")
    key = str(node.get("pathName") or name)
    plan = plan if plan is not None else {}
    current_path = f"{node_path}/{key}" if node_path else key
    slot = plan.get(current_path)
    game_object_id = slot.game_object_id if slot else sentinels.take()
    transform_id = slot.transform_id if slot else sentinels.take()

    skeleton_plan = inherited_skeleton
    skeleton_source = node.get("skeletonFromGlb")
    own_skeleton = None
    if skeleton_source:
        own_skeleton = plan_skeleton(str(skeleton_source), sentinels)
        skeleton_plan = own_skeleton

    component_specs = node.get("components")
    if component_specs is None:
        component_specs = []
    if not isinstance(component_specs, list):
        raise NewPrefabAssetError(f'node "{name}" components must be a list')

    rect = node.get("rectTransform")
    transform_class = RECT_TRANSFORM_CLASS_ID if isinstance(rect, dict) else TRANSFORM_CLASS_ID
    component_ids: list[tuple[int, int]] = [(transform_class, transform_id)]
    component_objects: list[BuiltObject] = []
    for spec_index, spec in enumerate(component_specs):
        if not isinstance(spec, dict):
            raise NewPrefabAssetError(f'node "{name}" has a non-object component')
        if slot is not None and spec_index < len(slot.component_ids):
            component_id = slot.component_ids[spec_index]
        else:
            component_id = sentinels.take()
        built = build_component(
            spec,
            name,
            game_object_id,
            component_id,
            sentinels,
            script_pool,
            registry,
            asset,
            external_refs,
            counts,
            skeleton_plan,
            plan,
            bundle_plans,
        )
        component_objects.append(built)
        component_ids.append((built.class_id, component_id))

    game_object_payload = write_typetree(
        {
            "m_Component": [
                [class_id, {"m_FileID": 0, "m_PathID": component_id}]
                for class_id, component_id in component_ids
            ],
            "m_Layer": int(node.get("layer", 0)),
            "m_Name": name,
            "m_Tag": int(node.get("tag", 0)),
            "m_IsActive": bool(node.get("active", True)),
        },
        load_schema("game_object_schema.json"),
        asset.endian,
    )
    game_object = BuiltObject(
        class_id=GAME_OBJECT_CLASS_ID,
        class_name="GameObject",
        name=name,
        payload=game_object_payload,
        local_id=game_object_id,
    )
    objects.append(game_object)
    counts["gameObjects"] += 1

    children_nodes = node.get("children") or []
    if not isinstance(children_nodes, list):
        raise NewPrefabAssetError(f'node "{name}" children must be a list')
    child_transform_ids: list[int] = []
    pending_children: list[dict[str, Any]] = [c for c in children_nodes]

    transform_placeholder = BuiltObject(
        class_id=transform_class,
        class_name="RectTransform" if transform_class == RECT_TRANSFORM_CLASS_ID else "Transform",
        name=name,
        payload=b"",
        local_id=transform_id,
    )
    objects.append(transform_placeholder)
    objects.extend(component_objects)
    counts["components"] += len(component_objects)

    for child in pending_children:
        child_transform_ids.append(
            build_node(
                child,
                transform_id,
                objects,
                sentinels,
                script_pool,
                registry,
                asset,
                external_refs,
                counts,
                inherited_skeleton=skeleton_plan,
                plan=plan,
                node_path=current_path,
                bundle_plans=bundle_plans,
            )
        )

    if own_skeleton is not None:
        child_transform_ids.extend(
            emit_skeleton(own_skeleton, transform_id, objects, asset, counts)
        )

    transform_fields = {
            "m_GameObject": {"fileId": 0, "pathId": game_object_id},
            "m_LocalRotation": build_vector(node.get("rotation"), 4, (0.0, 0.0, 0.0, 1.0)),
            "m_LocalPosition": build_vector(node.get("position"), 3, (0.0, 0.0, 0.0)),
            "m_LocalScale": build_vector(node.get("scale"), 3, (1.0, 1.0, 1.0)),
            "m_Children": [{"fileId": 0, "pathId": item} for item in child_transform_ids],
            "m_Father": {"fileId": 0, "pathId": parent_transform_id or 0},
    }
    if transform_class == RECT_TRANSFORM_CLASS_ID:
        transform_placeholder.payload = build_rect_transform(transform_fields, rect, asset)
    else:
        transform_placeholder.payload = build_transform(transform_fields, asset)
    return transform_id

def build_component(
    spec: dict[str, Any],
    node_name: str,
    game_object_id: int,
    component_id: int,
    sentinels: SentinelAllocator,
    script_pool: dict[tuple[str, str, str], int],
    registry: Any | None,
    asset: SerializedAsset,
    external_refs: dict[int, str],
    counts: dict[str, int],
    skeleton_plan: Any | None = None,
    plan: dict[str, Any] | None = None,
    bundle_plans: dict[str, Any] | None = None,
) -> BuiltObject:
    kind = str(spec.get("type") or "").strip()
    game_object_ref = pptr(0, game_object_id)
    if kind == "MonoBehaviour":
        counts["monoBehaviours"] += 1
        return build_mono_behaviour(
            spec,
            node_name,
            {"m_FileID": 0, "m_PathID": game_object_id},
            component_id,
            sentinels,
            script_pool,
            registry,
            asset,
            external_refs,
            plan,
            bundle_plans,
        )
    has_fields = isinstance(spec.get("fields"), dict)
    if kind == "MeshFilter" and not has_fields:
        payload = build_mesh_filter(
            {
                "m_GameObject": game_object_ref,
                "m_Mesh": build_reference(spec.get("mesh"), sentinels, external_refs, plan, bundle_plans),
            },
            asset,
        )
        return BuiltObject(MESH_FILTER_CLASS_ID, "MeshFilter", node_name, payload, component_id)
    if kind == "MeshRenderer" and not has_fields:
        fields = renderer_fields(spec, game_object_ref, sentinels, external_refs, plan, bundle_plans)
        fields["m_AdditionalVertexStreams"] = pptr(0, 0)
        payload = build_mesh_renderer(fields, asset)
        return BuiltObject(MESH_RENDERER_CLASS_ID, "MeshRenderer", node_name, payload, component_id)
    if kind == "SkinnedMeshRenderer" and not has_fields:
        fields = renderer_fields(spec, game_object_ref, sentinels, external_refs, plan, bundle_plans)
        fields["m_Mesh"] = build_reference(spec.get("mesh"), sentinels, external_refs, plan, bundle_plans)
        bones_source = spec.get("bonesFromGlb")
        if bones_source:
            if skeleton_plan is None or skeleton_plan.source != str(bones_source):
                raise NewPrefabAssetError(
                    f'bonesFromGlb "{bones_source}" needs the same GLB in "skeletonFromGlb" on this node'
                )
            fields["m_Bones"] = [
                pptr(0, item) for item in skeleton_plan.bone_transform_ids
            ]
            fields["m_RootBone"] = pptr(0, skeleton_plan.root_transform_id or 0)
        else:
            fields.setdefault("m_Bones", [])
            fields.setdefault("m_RootBone", pptr(0, 0))
        fields.setdefault("m_BlendShapeWeights", [])
        fields.setdefault(
            "m_AABB",
            {"m_Center": {"x": 0.0, "y": 0.0, "z": 0.0}, "m_Extent": {"x": 1.0, "y": 1.0, "z": 1.0}},
        )
        payload = build_skinned_mesh_renderer(fields, asset)
        return BuiltObject(
            SKINNED_MESH_RENDERER_CLASS_ID, "SkinnedMeshRenderer", node_name, payload, component_id
        )
    return build_generic_component(
        spec,
        kind,
        node_name,
        game_object_ref,
        component_id,
        sentinels,
        asset,
        external_refs,
        plan,
        bundle_plans,
    )

GENERIC_COMPONENT_CLASS_IDS = {
    name: class_id for class_id, name in CLASS_ID_NAMES.items()
}

EXTRA_SCHEMA_COMPONENTS = {
    82: "audio_source_schema.json",
    198: "particle_system_schema.json",
    **{
        class_id: EDITABLE_ASSET_SCHEMAS[class_id][1]
        for class_id in EDITABLE_COMPONENT_CLASS_IDS
    },
}

NODE_CLASS_IDS = {GAME_OBJECT_CLASS_ID, TRANSFORM_CLASS_ID, RECT_TRANSFORM_CLASS_ID}

PATCH_COMPONENT_SIZES = {
    20: 117,
    54: 36,
    64: 44,
    65: 52,
    108: 136,
    135: 44,
    136: 52,
    143: 64,
    167: 84,
    182: 40,
}

def build_generic_component(
    spec: dict[str, Any],
    kind: str,
    node_name: str,
    game_object_ref: dict[str, Any],
    component_id: int,
    sentinels: SentinelAllocator,
    asset: SerializedAsset,
    external_refs: dict[int, str],
    plan: dict[str, Any] | None = None,
    bundle_plans: dict[str, Any] | None = None,
) -> BuiltObject:
    from .map_component_repack import MapComponentRepackError, build_schema_component
    from .map_component_export import MAP_SCHEMA_COMPONENTS

    class_id = GENERIC_COMPONENT_CLASS_IDS.get(kind)
    if class_id is None:
        raise NewPrefabAssetError(
            f'node "{node_name}" has an unknown component "{kind}", use a Unity class name like BoxCollider'
        )
    if class_id in NODE_CLASS_IDS:
        raise NewPrefabAssetError(
            f'node "{node_name}" lists {kind}, which every node has already. Use "rectTransform" for a RectTransform'
        )
    if class_id not in COMPONENT_CLASS_IDS:
        raise NewPrefabAssetError(
            f'node "{node_name}" lists {kind}, which cant sit on a GameObject. Stage it as its own file instead'
        )
    fields = spec.get("fields")
    if fields is None:
        fields = {key: value for key, value in spec.items() if key not in {"type", "fields"}}
    if not isinstance(fields, dict):
        raise NewPrefabAssetError(f'component "{kind}" fields must be an object')
    fields = prepare_component_fields(fields, sentinels, external_refs, plan, bundle_plans)
    fields["m_GameObject"] = game_object_ref
    fields.setdefault("m_Enabled", True)

    try:
        schema_name = MAP_SCHEMA_COMPONENTS.get(class_id) or EXTRA_SCHEMA_COMPONENTS.get(class_id)
        if schema_name is not None:
            schema = load_schema(schema_name)
            payload = build_schema_component(
                fill_schema_defaults(schema, fields),
                asset,
                schema_name,
            )
        else:
            if class_id in {222, 223, 225}:
                payload = build_ui_component(class_id, fields, asset)
                return BuiltObject(class_id, kind, node_name, payload, component_id)
            builder = generic_builder(class_id)
            if builder is not None:
                payload = builder(fields, asset)
            elif class_id in PATCH_COMPONENT_SIZES:
                payload = build_patched_component(class_id, fields, asset)
            else:
                raise NewPrefabAssetError(
                    f'node "{node_name}" uses {kind}, which RWBY GE never puts on a GameObject'
                )
    except NewPrefabAssetError:
        raise
    except (MapComponentRepackError, KeyError, TypeError, ValueError) as exc:
        family = "asset-data" if class_id in EDITABLE_COMPONENT_CLASS_IDS else "map-json"
        raise NewPrefabAssetError(
            f'component "{kind}" on node "{node_name}" couldnt be written: {exc}. '
            f"Copy the full field set from an unpacked {family} component."
        ) from exc
    return BuiltObject(class_id, kind, node_name, payload, component_id)

def build_rect_transform(
    transform_fields: dict[str, Any],
    rect: dict[str, Any],
    asset: SerializedAsset,
) -> bytes:
    from .mono_repack import write_pptr
    from .ui_repack import write_rect_transform

    def vector(value: Any, length: int, default: tuple[float, ...]) -> list[float]:
        if isinstance(value, (list, tuple)) and len(value) >= length:
            return [float(item) for item in value[:length]]
        if isinstance(value, dict):
            labels = ("x", "y", "z", "w")
            return [float(value.get(labels[i], default[i])) for i in range(length)]
        return list(default)

    document = {
        "m_LocalRotation": vector(transform_fields.get("m_LocalRotation"), 4, (0.0, 0.0, 0.0, 1.0)),
        "m_LocalPosition": vector(transform_fields.get("m_LocalPosition"), 3, (0.0, 0.0, 0.0)),
        "m_LocalScale": vector(transform_fields.get("m_LocalScale"), 3, (1.0, 1.0, 1.0)),
        "m_Children": transform_fields.get("m_Children") or [],
        "m_Father": transform_fields.get("m_Father") or {},
        "m_AnchorMin": vector(rect.get("anchorMin"), 2, (0.0, 0.0)),
        "m_AnchorMax": vector(rect.get("anchorMax"), 2, (1.0, 1.0)),
        "m_AnchoredPosition": vector(rect.get("anchoredPosition"), 2, (0.0, 0.0)),
        "m_SizeDelta": vector(rect.get("sizeDelta"), 2, (0.0, 0.0)),
        "m_Pivot": vector(rect.get("pivot"), 2, (0.5, 0.5)),
    }
    writer = EndianWriter(asset.endian)
    write_pptr(writer, asset, {"m_PathID": transform_fields["m_GameObject"]["pathId"], "m_FileID": 0})
    write_rect_transform(writer, asset, document)
    return writer.bytes()

def build_ui_component(class_id: int, fields: dict[str, Any], asset: SerializedAsset) -> bytes:
    from .mono_repack import write_pptr
    from .ui_repack import write_canvas, write_canvas_group

    writer = EndianWriter(asset.endian)
    write_pptr(writer, asset, fields.get("m_GameObject") or {})
    if class_id == 223:
        write_canvas(writer, asset, fields)
    elif class_id == 225:
        write_canvas_group(writer, fields)
    return writer.bytes()

def generic_builder(class_id: int) -> Any | None:
    from .map_component_repack import (
        build_billboard_renderer,
        build_lod_group,
        build_particle_system_renderer,
        build_reflection_probe,
    )
    from .particle_system_export import build_particle_system
    from .scene_component_export import build_audio_source
    from .textmesh_repack import build_text_mesh

    return {
        23: build_mesh_renderer,
        33: build_mesh_filter,
        102: build_text_mesh,
        137: build_skinned_mesh_renderer,
        82: build_audio_source,
        198: build_particle_system,
        199: build_particle_system_renderer,
        205: build_lod_group,
        215: build_reflection_probe,
        227: build_billboard_renderer,
    }.get(class_id)

def build_patched_component(
    class_id: int,
    fields: dict[str, Any],
    asset: SerializedAsset,
) -> bytes:
    from .map_component_repack import (
        patch_audio_reverb_zone,
        patch_box_collider,
        patch_camera,
        patch_capsule_collider,
        patch_character_controller,
        patch_light,
        patch_mesh_collider,
        patch_rigidbody,
        patch_sphere_collider,
        patch_wind_zone,
    )

    writers = {
        20: patch_camera,
        54: patch_rigidbody,
        64: patch_mesh_collider,
        65: patch_box_collider,
        108: patch_light,
        135: patch_sphere_collider,
        136: patch_capsule_collider,
        143: patch_character_controller,
        167: patch_audio_reverb_zone,
        182: patch_wind_zone,
    }
    data = bytearray(PATCH_COMPONENT_SIZES[class_id])
    writers[class_id](data, fields, asset)
    return bytes(data)

def prepare_component_fields(
    value: Any,
    sentinels: SentinelAllocator,
    external_refs: dict[int, str],
    plan: dict[str, Any] | None = None,
    bundle_plans: dict[str, Any] | None = None,
) -> Any:
    if isinstance(value, dict):
        if value.get("node") is not None or is_prefab_reference(value):
            return build_reference(value, sentinels, external_refs, plan, bundle_plans)
        target = value.get("asset") or value.get("file")
        if target and len(value) == 1:
            return build_reference(str(target), sentinels, external_refs, plan, bundle_plans)
        return {
            key: prepare_component_fields(item, sentinels, external_refs, plan, bundle_plans)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            prepare_component_fields(item, sentinels, external_refs, plan, bundle_plans)
            for item in value
        ]
    if isinstance(value, str) and value.lower().endswith((".glb", ".png", ".json", ".mat", ".wav", ".ogg")):
        return build_reference(value, sentinels, external_refs)
    return value

@dataclass(slots=True)
class SkeletonPlan:
    source: str
    nodes: list[Any]
    game_object_ids: list[int]
    transform_ids: list[int]
    bone_transform_ids: list[int]
    root_transform_id: int | None

def plan_skeleton(source: str, sentinels: SentinelAllocator) -> SkeletonPlan:
    from .asset_bundle_mesh import NewMeshAssetError, read_glb_skeleton

    path = Path(source)
    if not path.is_file():
        raise NewPrefabAssetError(
            f'skeletonFromGlb "{source}" wasnt found, use the path of the GLB you staged'
        )
    try:
        nodes, bone_slots, root_slot = read_glb_skeleton(path)
    except NewMeshAssetError as exc:
        raise NewPrefabAssetError(f'skeletonFromGlb "{source}": {exc}') from exc
    game_object_ids = [sentinels.take() for _ in nodes]
    transform_ids = [sentinels.take() for _ in nodes]
    return SkeletonPlan(
        source=source,
        nodes=nodes,
        game_object_ids=game_object_ids,
        transform_ids=transform_ids,
        bone_transform_ids=[transform_ids[slot] for slot in bone_slots],
        root_transform_id=transform_ids[root_slot] if root_slot is not None else None,
    )

def emit_skeleton(
    plan: SkeletonPlan,
    parent_transform_id: int,
    objects: list[BuiltObject],
    asset: SerializedAsset,
    counts: dict[str, int],
) -> list[int]:
    children_of: dict[int, list[int]] = {}
    for slot, node in enumerate(plan.nodes):
        if node.parent is not None:
            children_of.setdefault(node.parent, []).append(slot)
    top_level: list[int] = []
    for slot, node in enumerate(plan.nodes):
        game_object_id = plan.game_object_ids[slot]
        transform_id = plan.transform_ids[slot]
        objects.append(
            BuiltObject(
                class_id=GAME_OBJECT_CLASS_ID,
                class_name="GameObject",
                name=node.name,
                payload=write_typetree(
                    {
                        "m_Component": [
                            [TRANSFORM_CLASS_ID, {"m_FileID": 0, "m_PathID": transform_id}]
                        ],
                        "m_Layer": 0,
                        "m_Name": node.name,
                        "m_Tag": 0,
                        "m_IsActive": True,
                    },
                    load_schema("game_object_schema.json"),
                    asset.endian,
                ),
                local_id=game_object_id,
            )
        )
        father = (
            plan.transform_ids[node.parent] if node.parent is not None else parent_transform_id
        )
        if node.parent is None:
            top_level.append(transform_id)
        objects.append(
            BuiltObject(
                class_id=TRANSFORM_CLASS_ID,
                class_name="Transform",
                name=node.name,
                payload=build_transform(
                    {
                        "m_GameObject": {"fileId": 0, "pathId": game_object_id},
                        "m_LocalRotation": {
                            "x": node.rotation[0],
                            "y": node.rotation[1],
                            "z": node.rotation[2],
                            "w": node.rotation[3],
                        },
                        "m_LocalPosition": {
                            "x": node.position[0],
                            "y": node.position[1],
                            "z": node.position[2],
                        },
                        "m_LocalScale": {
                            "x": node.scale[0],
                            "y": node.scale[1],
                            "z": node.scale[2],
                        },
                        "m_Children": [
                            {"fileId": 0, "pathId": plan.transform_ids[child]}
                            for child in children_of.get(slot, [])
                        ],
                        "m_Father": {"fileId": 0, "pathId": father},
                    },
                    asset,
                ),
                local_id=transform_id,
            )
        )
        counts["gameObjects"] += 1
        counts["components"] += 1
    counts["bones"] = counts.get("bones", 0) + len(plan.bone_transform_ids)
    return top_level

def build_mono_behaviour(
    spec: dict[str, Any],
    fallback_name: str,
    game_object_pptr: dict[str, int],
    object_id: int,
    sentinels: SentinelAllocator,
    script_pool: dict[tuple[str, str, str], int],
    registry: Any | None,
    asset: SerializedAsset,
    external_refs: dict[int, str],
    plan: dict[str, Any] | None = None,
    bundle_plans: dict[str, Any] | None = None,
) -> BuiltObject:
    script = spec.get("script")
    if not isinstance(script, dict):
        raise NewPrefabAssetError("MonoBehaviour needs a \"script\" object with class/namespace/assembly")
    class_name = str(script.get("class") or script.get("className") or "").strip()
    if not class_name:
        raise NewPrefabAssetError('MonoBehaviour "script" needs a class name')
    namespace = str(script.get("namespace") or "")
    assembly = str(script.get("assembly") or DEFAULT_ASSEMBLY)
    key = (assembly, namespace, class_name)
    script_id = script_pool.get(key)
    if script_id is None:
        script_id = sentinels.take()
        script_pool[key] = script_id

    writer = EndianWriter(asset.endian)
    writer.i32(int(game_object_pptr.get("m_FileID", 0)))
    writer.i64(int(game_object_pptr.get("m_PathID", 0)))
    writer.u8(1 if spec.get("enabled", True) else 0)
    writer.align()
    writer.i32(0)
    writer.i64(script_id)
    writer.aligned_string(str(spec.get("name") or ""))

    fields = spec.get("fields")
    if fields is None:
        fields = {}
    if not isinstance(fields, dict):
        raise NewPrefabAssetError(f"MonoBehaviour {class_name} fields must be an object")
    full_name = f"{namespace}.{class_name}" if namespace else class_name
    definitions = []
    if registry is not None:
        try:
            definitions = registry.serializable_fields(full_name)
        except Exception:
            definitions = []
    if definitions:
        prepared = prepare_reference_values(fields, sentinels, external_refs, plan, bundle_plans)
        for definition in definitions:
            if definition.name in prepared:
                value = prepared[definition.name]
            else:
                value = default_for_signature(registry, definition.signature, definition.name, full_name)
            write_managed_value(writer, asset, registry, definition.signature, value)
    elif fields:
        raise NewPrefabAssetError(
            f'MonoBehaviour "{full_name}" isnt a game script, leave "fields" empty for the BepInEx plugin to fill'
        )
    return BuiltObject(
        class_id=MONO_BEHAVIOUR_CLASS_ID,
        class_name="MonoBehaviour",
        name=str(spec.get("name") or fallback_name),
        payload=writer.bytes(),
        local_id=object_id,
        script=ScriptTypeRef(
            assembly=assembly, namespace=namespace, class_name=class_name, path_id=script_id
        ),
    )

def build_mono_asset(
    document: dict[str, Any],
    fallback_name: str,
    sentinels: SentinelAllocator,
    script_pool: dict[tuple[str, str, str], int],
    registry: Any | None,
    bundle_plans: dict[str, Any] | None = None,
) -> PrefabBuild:
    asset = bundle_stub_asset()
    external_refs: dict[int, str] = {}
    object_id = sentinels.take()
    name = str(document.get("name") or fallback_name)
    spec = dict(document)
    spec.setdefault("name", name)
    built = build_mono_behaviour(
        spec,
        name,
        {"m_FileID": 0, "m_PathID": 0},
        object_id,
        sentinels,
        script_pool,
        registry,
        asset,
        external_refs,
        None,
        bundle_plans,
    )
    built.container_listed = True
    built.name = name
    script = document.get("script") or {}
    details = {
        "script": {
            "assembly": str(script.get("assembly") or DEFAULT_ASSEMBLY),
            "namespace": str(script.get("namespace") or ""),
            "class": str(script.get("class") or script.get("className") or ""),
        },
    }
    return PrefabBuild(objects=[built], root_name=name, details=details, external_refs=external_refs)

def build_mono_script_object(reference: ScriptTypeRef) -> BuiltObject:
    writer = EndianWriter("<")
    writer.aligned_string(reference.class_name)
    writer.i32(0)
    writer.write(reference.monoscript_properties_hash())
    writer.aligned_string(reference.class_name)
    writer.aligned_string(reference.namespace)
    writer.aligned_string(reference.assembly)
    writer.bool(False)
    return BuiltObject(
        class_id=MONO_SCRIPT_CLASS_ID,
        class_name="MonoScript",
        name=reference.class_name,
        payload=writer.bytes(),
        local_id=reference.path_id,
    )

def renderer_fields(
    spec: dict[str, Any],
    game_object_ref: dict[str, int],
    sentinels: SentinelAllocator,
    external_refs: dict[int, str],
    plan: dict[str, Any] | None = None,
    bundle_plans: dict[str, Any] | None = None,
) -> dict[str, Any]:
    materials = spec.get("materials") or []
    if not isinstance(materials, list):
        raise NewPrefabAssetError("renderer materials must be a list")
    return {
        "m_GameObject": game_object_ref,
        "m_Enabled": bool(spec.get("enabled", True)),
        "m_CastShadows": int(spec.get("castShadows", 1)),
        "m_ReceiveShadows": bool(spec.get("receiveShadows", True)),
        "m_MotionVectors": int(spec.get("motionVectors", 1)),
        "m_LightProbeUsage": int(spec.get("lightProbeUsage", 1)),
        "m_ReflectionProbeUsage": int(spec.get("reflectionProbeUsage", 1)),
        "m_LightmapIndex": 65535,
        "m_LightmapIndexDynamic": 65535,
        "m_LightmapTilingOffset": {"x": 1.0, "y": 1.0, "z": 0.0, "w": 0.0},
        "m_LightmapTilingOffsetDynamic": {"x": 1.0, "y": 1.0, "z": 0.0, "w": 0.0},
        "m_Materials": [
            build_reference(item, sentinels, external_refs, plan, bundle_plans) for item in materials
        ],
        "m_SubsetIndices": [],
        "m_StaticBatchRoot": pptr(0, 0),
        "m_ProbeAnchor": pptr(0, 0),
        "m_LightProbeVolumeOverride": pptr(0, 0),
        "m_SortingLayerID": 0,
        "m_SortingOrder": 0,
        "m_Quality": 0,
        "m_UpdateWhenOffscreen": False,
        "m_SkinnedMotionVectors": True,
        "m_DirtyAABB": False,
    }

def build_reference(
    value: Any,
    sentinels: SentinelAllocator,
    external_refs: dict[int, str],
    plan: dict[str, Any] | None = None,
    bundle_plans: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if value in (None, ""):
        return pptr(0, 0)
    if isinstance(value, str):
        sentinel = sentinels.take()
        external_refs[sentinel] = value
        return pptr(0, sentinel)
    if isinstance(value, dict):
        if value.get("node") is not None or is_prefab_reference(value):
            if plan is None and not bundle_plans:
                raise NewPrefabAssetError("node references are only valid inside a prefab")
            return pptr(0, resolve_node_reference(value, plan or {}, bundle_plans))
        target = value.get("asset") or value.get("file")
        if target:
            sentinel = sentinels.take()
            external_refs[sentinel] = str(target)
            return pptr(0, sentinel)
        return pptr(
            int(value.get("fileId", value.get("m_FileID", 0))),
            int(value.get("pathId", value.get("m_PathID", 0))),
        )
    raise NewPrefabAssetError(f"unsupported reference value: {value!r}")

def pptr(file_id: int, path_id: int) -> dict[str, int]:
    return {
        "fileId": file_id,
        "pathId": path_id,
        "m_FileID": file_id,
        "m_PathID": path_id,
    }

def fill_schema_defaults(node: Any, value: Any) -> Any:
    if node.type_name == "pair":
        if isinstance(value, list) and len(value) == 2:
            return [
                fill_schema_defaults(node.children[0], value[0]),
                fill_schema_defaults(node.children[1], value[1]),
            ]
        return [
            fill_schema_defaults(node.children[0], None),
            fill_schema_defaults(node.children[1], None),
        ]
    if node.children and node.children[0].type_name == "Array":
        subtype = node.children[0].children[1]
        if isinstance(value, list):
            return [fill_schema_defaults(subtype, item) for item in value]
        return []
    if not node.children:
        if value is not None:
            return value
        if node.type_name == "string":
            return ""
        if node.type_name in {"bool"}:
            return False
        if node.type_name in {"float", "double"}:
            return 0.0
        return 0
    source = value if isinstance(value, dict) else {}
    return {
        child.name: fill_schema_defaults(child, source.get(child.name))
        for child in node.children
    }

def prepare_reference_values(
    value: Any,
    sentinels: SentinelAllocator,
    external_refs: dict[int, str],
    plan: dict[str, Any] | None = None,
    bundle_plans: dict[str, Any] | None = None,
) -> Any:
    if isinstance(value, dict):
        if value.get("node") is not None or is_prefab_reference(value):
            if plan is None and not bundle_plans:
                raise NewPrefabAssetError("node references are only valid inside a prefab")
            return {
                "m_FileID": 0,
                "m_PathID": resolve_node_reference(value, plan or {}, bundle_plans),
            }
        target = value.get("asset") or value.get("file")
        if target and len(value) == 1:
            sentinel = sentinels.take()
            external_refs[sentinel] = str(target)
            return {"m_FileID": 0, "m_PathID": sentinel}
        return {
            key: prepare_reference_values(item, sentinels, external_refs, plan, bundle_plans)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            prepare_reference_values(item, sentinels, external_refs, plan, bundle_plans)
            for item in value
        ]
    return value

def default_for_signature(registry: Any, sig: Any, field_name: str, owner: str) -> Any:
    kind = getattr(sig, "kind", "")
    name = getattr(sig, "name", "")
    if kind == "primitive":
        return False if name == "bool" else 0
    if kind == "string":
        return ""
    if kind in {"szarray", "array", "list"}:
        return []
    try:
        if registry.is_unity_object(sig):
            return {"m_FileID": 0, "m_PathID": 0}
        if registry.is_unity_event(sig) or name in UNITY_VALUE_TYPES | UNITY_SERIALIZABLE_TYPES:
            return {}
        if registry.is_enum(sig):
            return 0
        nested = registry.serializable_fields_for_signature(sig)
    except Exception:
        nested = []
    if nested:
        return {
            item.name: default_for_signature(registry, item.signature, item.name, owner)
            for item in nested
        }
    raise NewPrefabAssetError(
        f'{owner} field "{field_name}" has no default Sunflowyr can synthesize, set it explicitly'
    )

def build_vector(value: Any, length: int, default: tuple[float, ...]) -> dict[str, float]:
    labels = ("x", "y", "z", "w")
    if value is None:
        values = default
    elif isinstance(value, (list, tuple)) and len(value) == length:
        values = tuple(float(item) for item in value)
    elif isinstance(value, dict):
        values = tuple(float(value.get(labels[i], default[i])) for i in range(length))
    else:
        raise NewPrefabAssetError(f"expected {length} numbers for a transform vector")
    return {labels[index]: values[index] for index in range(length)}

def write_character_template(folder: Path, slug: str = "sunflowyr_newchar") -> list[Path]:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    character_file = folder / f"{slug}_character.mono.json"
    team_file = folder / f"{slug}_team.mono.json"
    prefab_file = folder / f"{slug}.prefab.json"

    character = {
        "format": MONO_ASSET_FORMAT,
        "name": "SunflowyrNewCharacter",
        "script": {"assembly": DEFAULT_ASSEMBLY, "namespace": "", "class": "PlayableCharacterDefinition"},
        "comment": (
            "ID must be a unique string. Prefab is omitted: for a first test with "
            "an existing character, let the BepInEx plugin assign the game's own Ruby/Yang prefab "
            'at runtime. To ship a new character instead, set "Prefab": '
            f'{{"asset": "{prefab_file.name}"}} and stage that prefab JSON too. '
            "Every field you omit is written with a safe default by Sunflowyr."
        ),
        "fields": {
            "ID": f"{slug}.character",
            "Name": "Sunflowyr New Character",
            "NameLocalizationKey": "",
        },
    }
    team = {
        "format": MONO_ASSET_FORMAT,
        "name": "SunflowyrNewTeam",
        "script": {"assembly": DEFAULT_ASSEMBLY, "namespace": "", "class": "PlayableTeamDefinition"},
        "comment": (
            "TeamRoster holds up to four PlayableCharacterDefinition references by staged file name."
        ),
        "fields": {
            "ID": f"{slug}.team",
            "TeamName": "Team Sunflowyr",
            "OrderNumber": 99,
            "TeamRoster": [{"asset": character_file.name}],
        },
    }
    prefab = {
        "format": PREFAB_FORMAT,
        "name": "SunflowyrNewCharacter",
        "comment": (
            "The root MonoBehaviour must be a script your BepInEx assembly provides (derived from "
            "PlayerCharacter). Sunflowyr cant lay out its fields, so leave them to the plugin. "
            "Components accept any RWBY GE class name (MeshFilter, MeshRenderer, "
            "SkinnedMeshRenderer, ParticleSystem, ParticleSystemRenderer, BoxCollider, "
            "CapsuleCollider, AudioSource, Animation, Light, LODGroup, MonoBehaviour, etc). put "
            "class fields under \"fields\" and omit what you dont care about. Mesh/material "
            "values reference other staged input files by name. To rebuild a character skeleton, "
            'add "skeletonFromGlb": "<path to a Sunflowyr-exported skinned GLB>" on the node and '
            '"bonesFromGlb": "<same path>" on its SkinnedMeshRenderer.'
        ),
        "root": {
            "name": "SunflowyrNewCharacter",
            "layer": 0,
            "active": True,
            "position": [0.0, 0.0, 0.0],
            "components": [
                {
                    "type": "MonoBehaviour",
                    "script": {
                        "assembly": "YourBepInExPlugin",
                        "namespace": "",
                        "class": "YourCharacterScript",
                    },
                }
            ],
            "children": [],
        },
    }
    written: list[Path] = []
    for path, document in (
        (character_file, character),
        (team_file, team),
        (prefab_file, prefab),
    ):
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written

def substitute_sentinels(payload: bytes, mapping: dict[int, int]) -> bytes:
    if not mapping:
        return payload
    marker = struct.pack("<I", SENTINEL_BASE >> 32)
    data = bytearray(payload)
    start = 0
    while True:
        index = data.find(marker, start)
        if index < 0:
            return bytes(data)
        value_start = index - 4
        if value_start >= 0:
            real = mapping.get(struct.unpack_from("<q", data, value_start)[0])
            if real is not None:
                struct.pack_into("<q", data, value_start, real)
                start = value_start + 8
                continue
        start = index + 4
