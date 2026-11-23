from __future__ import annotations
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from .binary import EndianReader
from .class_ids import CLASS_ID_NAMES, class_name
from .model_context import ModelContextIndex, ObjectKey, PPtr
from .serialized import SerializedAsset
from .unity_typetree_codec import load_schema, read_typetree

MAP_FORMAT = "sunflowyr-replacement-map"

RENDERER_MEMBER = "sharedMaterials"
NOT_SCANNED_NOTE = [
    "Font.m_Texture / Font.m_DefaultMaterial",
    "TerrainData splat textures",
    "LensFlare/Projector/Skybox material/texture slots",
    "ParticleSystem module references beyond the renderer material",
    "objects instantiated at runtime (re-apply the map on scene load/spawn)",
]

class ReplacementMapError(ValueError):
    pass

@dataclass(frozen=True, slots=True)
class ReplacementTarget:
    container: str
    path_id: int
    class_id: int
    name: str
    bundle_asset_path: str
    bundle_class_name: str

@dataclass(slots=True)
class MapStats:
    entries: int = 0
    unanchored: int = 0
    mono_objects_scanned: int = 0

def build_replacement_map(
    context: ModelContextIndex,
    targets: list[ReplacementTarget],
    *,
    bundle_name: str = "",
    scan_mono: bool = False,
    mono_scripts: dict | None = None,
    managed_registry: Any | None = None,
) -> tuple[dict[str, Any], MapStats]:
    stats = MapStats()
    entries: list[dict[str, Any]] = []
    unanchored: list[dict[str, Any]] = []

    for target in targets:
        key = target_key(context, target)
        if key is None:
            raise ReplacementMapError(
                f"replacement target {target.container} path {target.path_id} wasnt found in the game data"
            )
        if target.class_id == 21:
            scan_material_target(context, target, key, entries, unanchored)
        elif target.class_id in (28, 89):
            scan_texture_target(context, target, key, entries, unanchored)
        elif target.class_id == 43:
            scan_mesh_target(context, target, key, entries, unanchored)
        elif target.class_id == 83:
            scan_audio_target(context, target, key, entries, unanchored)
        elif target.class_id == 74:
            scan_animation_target(context, target, key, entries, unanchored)
        elif target.class_id == 213:
            if not scan_mono:
                unanchored.append(
                    build_unanchored(target, "Sprite references live in UI MonoBehaviour fields, run with --scan-mono")
                )
        else:
            unanchored.append(
                build_unanchored(target, f"{class_name(target.class_id)} reference scanning isnt implemented")
            )

    if scan_mono:
        stats.mono_objects_scanned = scan_mono_fields(
            context,
            targets,
            entries,
            mono_scripts or {},
            managed_registry,
        )

    stats.entries = len(entries)
    stats.unanchored = len(unanchored)
    document = {
        "format": MAP_FORMAT,
        "version": 1,
        "game": "rwby-ge",
        "tool": "Sunflowyr Engine",
        "createdUtc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bundleName": bundle_name,
        "pluginNote": (
            "Each entry is: where the reference lives and what to put there. "
            "'scenePath' is the GameObject path (same shape RuntimeUnityEditor shows), "
            "'component' is the component on it, and 'memberPath' is the reflection path from "
            "that component to the reference, i.e., sharedMaterials[0].mainTexture or "
            'sharedMaterials[0].SetTexture("_BumpMap"). Assign bundle.LoadAsset(asset) there. '
            "Only _MainTex has a C# property (mainTexture). every other slot must go through "
            "SetTexture, which is why memberPath names the call explicitly. "
            "'scope' says what you are touching: 'shared' mutates the shared asset and affects "
            "every user of it, 'instance' touches only this component, 'materialAsset' entries "
            "have no scenePath. find the Material by name and set the slot once, which "
            "rebinds every renderer using it. "
            "'match' carries fallback identifiers if the exact path misses. "
            "Entries whose container isnt a levelN scene are paths inside prefabs, apply them "
            "to instantiated clones. Re-apply on scene load and after spawns. GameObject.Find "
            "misses inactive objects, so walk scene roots instead."
        ),
        "targets": [
            {
                "replaces": {"container": t.container, "pathId": t.path_id, "className": class_name(t.class_id), "name": t.name},
                "asset": t.bundle_asset_path,
                "assetClass": t.bundle_class_name,
            }
            for t in targets
        ],
        "entries": entries,
        "unanchored": unanchored,
        "coverage": {
            "scanned": [
                "MeshRenderer/SkinnedMeshRenderer/ParticleSystemRenderer/Trail/Line/Billboard sharedMaterials",
                "Material texture slots -> renderers using that material",
                "MeshFilter.sharedMesh, SkinnedMeshRenderer.sharedMesh, MeshCollider.sharedMesh",
                "AudioSource.clip",
                "Animation component clip lists (informational)",
                "MonoBehaviour typed fields (only with scan_mono)" if not scan_mono else "MonoBehaviour typed fields",
            ],
            "notScanned": NOT_SCANNED_NOTE,
        },
    }
    return document, stats

TARGETS_FORMAT = "sunflowyr-rebind-targets"
REBIND_ASSET_PATH = "assets/sunflowyr/rebind_map.json"
REBIND_ASSET_NAME = "sunflowyr_rebind_map"

def resolve_targets_from_taildata(
    replace_paths: list[str],
    taildata_paths: list[str],
    manifest_assets: list[dict[str, Any]],
) -> tuple[list[ReplacementTarget], list[str]]:
    from .catalog import (
        collect_catalog_paths,
        collect_payload_files,
        find_catalog_record,
        load_catalog_indexes,
    )

    problems: list[str] = []
    staged = collect_payload_files(replace_paths)
    if not staged:
        return [], ["no exported originals were staged"]
    catalogs = load_catalog_indexes(
        collect_catalog_paths(replace_paths, staged, taildata_paths)
    )
    if not catalogs:
        return [], [
            f"no {'sunflowyr_taildata.json'} was found beside the staged files, "
            "add the catalog explicitly"
        ]
    by_source = {
        str(item.get("sourceFile") or "").casefold(): item
        for item in manifest_assets
        if item.get("sourceFile")
    }
    targets: list[ReplacementTarget] = []
    seen: set[tuple[str, int]] = set()
    for item in staged:
        catalog_path, record, output = find_catalog_record(item, catalogs)
        if not record:
            problems.append(f"{item.path.name}: no taildata record matches")
            continue
        bundle_asset = by_source.get(item.path.name.casefold())
        if bundle_asset is None:
            problems.append(
                f"{item.path.name}: stage this same file as a bundle input so the map knows "
                "which new asset replaces it"
            )
            continue
        container = str(record.get("appendTarget") or record.get("container") or "")
        path_id = int(record.get("pathId", 0))
        key = (container.casefold(), path_id)
        if key in seen:
            continue
        seen.add(key)
        targets.append(
            ReplacementTarget(
                container=container,
                path_id=path_id,
                class_id=int(record.get("classId", 0)),
                name=str(record.get("name") or item.path.stem),
                bundle_asset_path=str(bundle_asset.get("assetPath") or ""),
                bundle_class_name=str(bundle_asset.get("className") or ""),
            )
        )
    return targets, problems

def resolve_targets_by_name(
    context: ModelContextIndex,
    requests: list[dict[str, Any]],
    manifest_assets: list[dict[str, Any]],
) -> tuple[list[ReplacementTarget], list[str]]:
    by_path = {
        str(item.get("assetPath") or "").casefold(): item
        for item in manifest_assets
        if item.get("assetPath")
    }
    by_name = {
        str(item.get("name") or "").casefold(): item
        for item in manifest_assets
        if item.get("name")
    }
    wanted_classes: dict[int, list[dict[str, Any]]] = {}
    problems: list[str] = []
    for request in requests:
        class_name_text = str(request.get("className") or "").strip()
        class_id = next(
            (cid for cid, name in CLASS_ID_NAMES.items() if name.casefold() == class_name_text.casefold()),
            None,
        )
        if class_id is None:
            problems.append(f'unknown className "{class_name_text}"')
            continue
        if not str(request.get("replaceName") or "").strip():
            problems.append(f'{class_name_text} target is missing "replaceName"')
            continue
        wanted_classes.setdefault(class_id, []).append(request)

    found: dict[tuple[int, str], list[ObjectKey]] = {}
    for asset_key, asset in context.assets.items():
        objects = [obj for obj in asset.objects if obj.class_id in wanted_classes]
        if not objects:
            continue
        with asset.path.open("rb") as handle:
            reader = EndianReader(handle, asset.endian)
            for obj in objects:
                try:
                    from .objects import describe_object

                    name = describe_object(asset, reader, obj).name
                except Exception:
                    continue
                if not name:
                    continue
                found.setdefault((obj.class_id, name.casefold()), []).append((asset_key, obj.path_id))

    targets: list[ReplacementTarget] = []
    for class_id, requests_for_class in wanted_classes.items():
        for request in requests_for_class:
            replace_name = str(request.get("replaceName")).strip()
            matches = found.get((class_id, replace_name.casefold()), [])
            if not matches:
                problems.append(
                    f'no {class_name(class_id)} named "{replace_name}" exists in the game data'
                )
                continue
            with_asset = str(request.get("withAsset") or "").strip()
            bundle_asset = by_path.get(with_asset.casefold()) or by_name.get(with_asset.casefold())
            if bundle_asset is None:
                problems.append(
                    f'"{with_asset}" isnt an asset in this bundle (use its assetPath or name)'
                )
                continue
            for asset_key, path_id in matches:
                targets.append(
                    ReplacementTarget(
                        container=asset_key,
                        path_id=path_id,
                        class_id=class_id,
                        name=replace_name,
                        bundle_asset_path=str(bundle_asset.get("assetPath") or ""),
                        bundle_class_name=str(bundle_asset.get("className") or ""),
                    )
                )
    return targets, problems

def target_key(context: ModelContextIndex, target: ReplacementTarget) -> ObjectKey | None:
    normalized = target.container.replace("\\", "/").lower()
    asset_key = context.aliases.get(normalized) or context.aliases.get(Path(normalized).name)
    if asset_key is None:
        return None
    key = (asset_key, target.path_id)
    return key if key in context.objects else None

def scan_material_target(
    context: ModelContextIndex,
    target: ReplacementTarget,
    key: ObjectKey,
    entries: list[dict[str, Any]],
    unanchored: list[dict[str, Any]],
) -> None:
    found = False
    for renderer in context.renderers:
        for index, pointer in enumerate(renderer.materials):
            if context.resolve(renderer.asset_key, pointer) != key:
                continue
            entry = renderer_entry(context, renderer, target)
            if entry is None:
                continue
            entry.update(
                {
                    "action": "setMaterial",
                    "materialIndex": index,
                    "memberPath": f"sharedMaterials[{index}]",
                    "scope": "shared",
                }
            )
            entries.append(entry)
            found = True
    if not found:
        unanchored.append(build_unanchored(target, "no renderer references this material"))

def scan_texture_target(
    context: ModelContextIndex,
    target: ReplacementTarget,
    key: ObjectKey,
    entries: list[dict[str, Any]],
    unanchored: list[dict[str, Any]],
) -> None:
    found = False
    for material_key, material in context.materials.items():
        slots = [
            slot
            for slot, record in material.textures.items()
            if context.resolve(material.asset_key, record.texture) == key
        ]
        if not slots:
            continue
        material_anchored = False
        for renderer in context.renderers:
            for index, pointer in enumerate(renderer.materials):
                if context.resolve(renderer.asset_key, pointer) != material_key:
                    continue
                entry = renderer_entry(context, renderer, target)
                if entry is None:
                    continue
                for slot in slots:
                    slot_entry = dict(entry)
                    slot_entry.update(
                        {
                            "action": "setMaterialTexture",
                            "materialIndex": index,
                            "textureSlot": slot,
                            "materialName": material.name,
                            "memberPath": texture_member_path(index, slot),
                            "scope": "shared",
                        }
                    )
                    entries.append(slot_entry)
                material_anchored = True
                found = True
        for slot in slots:
            entries.append(
                {
                    "container": material_key[0],
                    "scenePath": "",
                    "component": "Material",
                    "action": "setMaterialAssetTexture",
                    "materialName": material.name,
                    "textureSlot": slot,
                    "memberPath": texture_member_path(None, slot),
                    "scope": "materialAsset",
                    "match": {"materialName": material.name},
                    **asset_fields(target),
                }
            )
            found = True
        if not material_anchored:
            unanchored.append(
                build_unanchored(
                    target,
                    f'texture is used by material "{material.name}" '
                    f"({material_key[0]} path {material_key[1]}) which no scanned renderer uses, "
                    "the materialAsset entry can still rebind it without a scene path",
                )
            )
            found = True
    if not found:
        unanchored.append(build_unanchored(target, "no scanned material references this texture"))

def scan_mesh_target(
    context: ModelContextIndex,
    target: ReplacementTarget,
    key: ObjectKey,
    entries: list[dict[str, Any]],
    unanchored: list[dict[str, Any]],
) -> None:
    found = False
    for game_object_key, mesh_filter in context.mesh_filters_by_game_object.items():
        if context.resolve(mesh_filter.asset_key, mesh_filter.mesh) != key:
            continue
        located = scene_path_for_game_object(context, game_object_key)
        if located is None:
            continue
        container, scene_path, active = located
        entries.append(
            {
                "container": container,
                "scenePath": scene_path,
                "gameObjectActive": active,
                "component": "MeshFilter",
                "action": "setMember",
                "member": "sharedMesh",
                "memberPath": "sharedMesh",
                "scope": "shared",
                **asset_fields(target),
            }
        )
        found = True
    for renderer in context.renderers:
        if not renderer.skinned or renderer.mesh is None:
            continue
        if context.resolve(renderer.asset_key, renderer.mesh) != key:
            continue
        entry = renderer_entry(context, renderer, target)
        if entry is None:
            continue
        entry.update(
            {
                "action": "setMember",
                "member": "sharedMesh",
                "memberPath": "sharedMesh",
                "scope": "shared",
            }
        )
        entries.append(entry)
        found = True
    found = scan_mesh_colliders(context, target, key, entries) or found
    if not found:
        unanchored.append(build_unanchored(target, "no MeshFilter/SkinnedMeshRenderer/MeshCollider references this mesh"))

def scan_mesh_colliders(
    context: ModelContextIndex,
    target: ReplacementTarget,
    key: ObjectKey,
    entries: list[dict[str, Any]],
) -> bool:
    found = False
    for asset_key, asset in context.assets.items():
        colliders = [obj for obj in asset.objects if obj.class_id == 64]
        if not colliders:
            continue
        with asset.path.open("rb") as handle:
            reader = EndianReader(handle, asset.endian)
            for obj in colliders:
                if obj.byte_size < 44:
                    continue
                reader.seek(obj.byte_start)
                game_object = PPtr(reader.i32(), reader.i64())
                reader.seek(obj.byte_start + 32)
                mesh = PPtr(reader.i32(), reader.i64())
                if context.resolve(asset_key, mesh) != key:
                    continue
                game_object_key = context.resolve(asset_key, game_object)
                located = (
                    scene_path_for_game_object(context, game_object_key)
                    if game_object_key is not None
                    else None
                )
                if located is None:
                    continue
                container, scene_path, active = located
                entries.append(
                    {
                        "container": container,
                        "scenePath": scene_path,
                        "gameObjectActive": active,
                        "component": "MeshCollider",
                        "action": "setMember",
                        "member": "sharedMesh",
                        "memberPath": "sharedMesh",
                        "scope": "shared",
                        **asset_fields(target),
                    }
                )
                found = True
    return found

def scan_audio_target(
    context: ModelContextIndex,
    target: ReplacementTarget,
    key: ObjectKey,
    entries: list[dict[str, Any]],
    unanchored: list[dict[str, Any]],
) -> None:
    found = False
    schema = load_schema("audio_source_schema.json")
    for asset_key, asset in context.assets.items():
        sources = [obj for obj in asset.objects if obj.class_id == 82]
        if not sources:
            continue
        with asset.path.open("rb") as handle:
            for obj in sources:
                handle.seek(obj.byte_start)
                data = handle.read(obj.byte_size)
                try:
                    fields = read_typetree(data, schema, asset.endian)
                except Exception:
                    continue
                clip = pptr_from_fields(fields.get("m_audioClip"))
                if clip is None or context.resolve(asset_key, clip) != key:
                    continue
                game_object = pptr_from_fields(fields.get("m_GameObject"))
                game_object_key = (
                    context.resolve(asset_key, game_object) if game_object is not None else None
                )
                located = (
                    scene_path_for_game_object(context, game_object_key)
                    if game_object_key is not None
                    else None
                )
                if located is None:
                    continue
                container, scene_path, active = located
                entries.append(
                    {
                        "container": container,
                        "scenePath": scene_path,
                        "gameObjectActive": active,
                        "component": "AudioSource",
                        "action": "setMember",
                        "member": "clip",
                        "memberPath": "clip",
                        "scope": "instance",
                        **asset_fields(target),
                    }
                )
                found = True
    if not found:
        unanchored.append(build_unanchored(target, "no AudioSource references this clip"))

def scan_animation_target(
    context: ModelContextIndex,
    target: ReplacementTarget,
    key: ObjectKey,
    entries: list[dict[str, Any]],
    unanchored: list[dict[str, Any]],
) -> None:
    found = False
    for record in context.animation_records:
        clip_keys = [context.resolve(record.asset_key, clip) for clip in record.clips]
        if key not in clip_keys:
            continue
        game_object_key = context.resolve(record.asset_key, record.game_object)
        located = (
            scene_path_for_game_object(context, game_object_key)
            if game_object_key is not None
            else None
        )
        if located is None:
            continue
        container, scene_path, active = located
        entries.append(
            {
                "container": container,
                "scenePath": scene_path,
                "gameObjectActive": active,
                "component": "Animation",
                "action": "replaceAnimationClip",
                "clipName": target.name,
                "memberPath": f'GetClip("{target.name}")',
                "scope": "shared",
                **asset_fields(target),
            }
        )
        found = True
    if not found:
        unanchored.append(
            build_unanchored(target, "no Animation component references this clip")
        )

def scan_mono_fields(
    context: ModelContextIndex,
    targets: list[ReplacementTarget],
    entries: list[dict[str, Any]],
    mono_scripts: dict,
    managed_registry: Any | None,
) -> int:
    from .mono_export import read_monobehaviour_json

    target_keys: dict[ObjectKey, ReplacementTarget] = {}
    for target in targets:
        key = target_key(context, target)
        if key is not None:
            target_keys[key] = target
    if not target_keys:
        return 0

    scanned = 0
    for asset_key, asset in context.assets.items():
        behaviours = [obj for obj in asset.objects if obj.class_id == 114]
        if not behaviours:
            continue
        with asset.path.open("rb") as handle:
            reader = EndianReader(handle, asset.endian)
            for obj in behaviours:
                try:
                    document, conversion = read_monobehaviour_json(
                        asset, reader, obj, mono_scripts, managed_registry
                    )
                except Exception:
                    continue
                if conversion.get("format") != "monobehaviour_json_typed_managed":
                    continue
                scanned += 1
                script = document.get("solarScript") or {}
                script_name = str(
                    script.get("class_name") or script.get("full_name") or "MonoBehaviour"
                )
                hits: list[tuple[str, ReplacementTarget]] = []
                walk_mono_fields(context, asset_key, document, "", target_keys, hits)
                if not hits:
                    continue
                game_object = pptr_from_mono(document.get("m_GameObject"))
                game_object_key = (
                    context.resolve(asset_key, game_object) if game_object is not None else None
                )
                located = (
                    scene_path_for_game_object(context, game_object_key)
                    if game_object_key is not None
                    else None
                )
                if located is None:
                    continue
                container, scene_path, active = located
                for member, target in hits:
                    entries.append(
                        {
                            "container": container,
                            "scenePath": scene_path,
                            "gameObjectActive": active,
                            "component": script_name,
                            "action": "setMember",
                            "member": member,
                            "memberPath": member,
                            "scope": "instance",
                            **asset_fields(target),
                        }
                    )
    return scanned

MONO_HEADER_FIELDS = {"m_GameObject", "m_Enabled", "m_Script", "m_Name", "solarScript", "solarRawSerializedFields"}

def walk_mono_fields(
    context: ModelContextIndex,
    asset_key: str,
    value: Any,
    member: str,
    target_keys: dict[ObjectKey, ReplacementTarget],
    hits: list[tuple[str, ReplacementTarget]],
) -> None:
    if isinstance(value, dict):
        pointer = pptr_from_mono(value)
        if pointer is not None and member:
            resolved = context.resolve(asset_key, pointer)
            if resolved is not None and resolved in target_keys:
                hits.append((member, target_keys[resolved]))
            return
        for name, item in value.items():
            if not member and name in MONO_HEADER_FIELDS:
                continue
            walk_mono_fields(
                context,
                asset_key,
                item,
                f"{member}.{name}" if member else str(name),
                target_keys,
                hits,
            )
    elif isinstance(value, list):
        for index, item in enumerate(value):
            walk_mono_fields(context, asset_key, item, f"{member}[{index}]", target_keys, hits)

def pptr_from_mono(value: Any) -> PPtr | None:
    if not isinstance(value, dict):
        return None
    if "m_FileID" in value and "m_PathID" in value and len(value) <= 4:
        try:
            return PPtr(int(value["m_FileID"]), int(value["m_PathID"]))
        except (TypeError, ValueError):
            return None
    if "file_id" in value and "path_id" in value:
        try:
            return PPtr(int(value["file_id"]), int(value["path_id"]))
        except (TypeError, ValueError):
            return None
    return None

def pptr_from_fields(value: Any) -> PPtr | None:
    if not isinstance(value, dict):
        return None
    try:
        return PPtr(int(value.get("m_FileID", 0)), int(value.get("m_PathID", 0)))
    except (TypeError, ValueError):
        return None

def texture_member_path(material_index: int | None, slot: str) -> str:
    prefix = "" if material_index is None else f"sharedMaterials[{material_index}]."
    if slot == "_MainTex":
        return f"{prefix}mainTexture"
    return f'{prefix}SetTexture("{slot}")'

def renderer_entry(
    context: ModelContextIndex,
    renderer: Any,
    target: ReplacementTarget,
) -> dict[str, Any] | None:
    game_object_key = context.resolve(renderer.asset_key, renderer.game_object)
    if game_object_key is None:
        return None
    located = scene_path_for_game_object(context, game_object_key)
    if located is None:
        return None
    container, scene_path, active = located
    renderer_object = context.objects.get(renderer.key)
    component = class_name(renderer_object.class_id) if renderer_object is not None else "Renderer"
    return {
        "container": container,
        "scenePath": scene_path,
        "gameObjectActive": active,
        "component": component,
        "match": {
            "gameObjectName": scene_path.rsplit("/", 1)[-1],
            "component": component,
        },
        **asset_fields(target),
    }

def scene_path_for_game_object(
    context: ModelContextIndex,
    game_object_key: ObjectKey,
) -> tuple[str, str, bool] | None:
    transform_key = context.transforms_by_game_object.get(game_object_key)
    if transform_key is None:
        return None
    names: list[str] = []
    visited: set[ObjectKey] = set()
    current: ObjectKey | None = transform_key
    while current is not None and current not in visited:
        visited.add(current)
        transform = context.transforms.get(current)
        if transform is None:
            break
        names.append(transform.name or f"Transform_{current[1]}")
        current = context.resolve(transform.asset_key, transform.father)
    if not names:
        return None
    record = context.game_objects.get(game_object_key)
    active = record.active if record is not None else True
    container = game_object_key[0]
    return container, "/".join(reversed(names)), active

def asset_fields(target: ReplacementTarget) -> dict[str, Any]:
    return {
        "asset": target.bundle_asset_path,
        "assetClass": target.bundle_class_name,
        "replaces": {
            "container": target.container,
            "pathId": target.path_id,
            "className": class_name(target.class_id),
            "name": target.name,
        },
    }

def build_unanchored(target: ReplacementTarget, reason: str) -> dict[str, Any]:
    return {**asset_fields(target), "reason": reason}

def write_replacement_map(document: dict[str, Any], out_path: Path) -> None:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
