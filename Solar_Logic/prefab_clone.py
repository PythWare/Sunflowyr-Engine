from __future__ import annotations
import json, re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from .binary import EndianReader, read_range
from .class_ids import class_name
from .mesh_glb_export import export_mesh_glb
from .asset_bundle_shader import build_shader_name_index, export_game_shader
from .model_context import ModelContextIndex, ObjectKey
from .mono_export import build_monoscript_index, read_monoscript
from .objects import describe_object
from .serialized import SerializedAsset
from .editable_object_export import decorate_pointers
from .font_export import font_extension, read_font_json
from .sprite_export import read_sprite
from .texture_convert import TextureDecodeError, save_png

class PrefabCloneError(ValueError):
    pass

@dataclass(slots=True)
class CloneResult:
    prefab_path: Path
    folder: Path
    root_name: str
    files: list[Path] = field(default_factory=list)
    meshes: int = 0
    materials: int = 0
    shaders: int = 0
    textures: int = 0
    sprites: int = 0
    fonts: int = 0
    editable_assets: int = 0
    scripts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    nodes: int = 0
    animations: int = 0
    data_assets: int = 0
    audio: int = 0
    extra_prefabs: list[str] = field(default_factory=list)
    external_refs: list[str] = field(default_factory=list)
    exported_files: dict = field(default_factory=dict)
    rebinds: list[dict] = field(default_factory=list)
    report_path: Path | None = None

def find_character_roots(context: ModelContextIndex) -> list[tuple[ObjectKey, str]]:
    roots: dict[ObjectKey, str] = {}
    for renderer in context.renderers:
        if not renderer.skinned:
            continue
        game_object_key = context.resolve(renderer.asset_key, renderer.game_object)
        if game_object_key is None:
            continue
        transform_key = context.transforms_by_game_object.get(game_object_key)
        root_key = find_root_transform(context, transform_key) if transform_key else None
        if root_key is None:
            continue
        transform = context.transforms.get(root_key)
        if transform is None:
            continue
        owner = context.resolve(transform.asset_key, transform.game_object)
        if owner is None:
            continue
        roots.setdefault(owner, context.game_object_names.get(owner, "") or f"Object_{owner[1]}")
    return sorted(roots.items(), key=lambda item: item[1].casefold())

def find_root_transform(context: ModelContextIndex, key: ObjectKey | None) -> ObjectKey | None:
    seen: set[ObjectKey] = set()
    current = key
    while current is not None and current not in seen:
        seen.add(current)
        transform = context.transforms.get(current)
        if transform is None:
            return current
        parent = context.resolve(transform.asset_key, transform.father)
        if parent is None or parent not in context.transforms:
            return current
        current = parent
    return current

EDITABLE_BUNDLE_CLASSES = {91, 241, 243, 245}
EXPORTABLE_ASSET_CLASSES = {21, 28, 43, 74, 83, 89, 128, 134, 213} | EDITABLE_BUNDLE_CLASSES
REBIND_SIDECAR_NAME = "sunflowyr_rebinds.json"
SKIP_COMPONENT_CLASSES = {4, 224}

def clone_character_exact(
    context: ModelContextIndex,
    root_key: ObjectKey,
    out_folder: Path,
    *,
    include_scripts: bool = True,
    managed_registry: Any | None = None,
    mono_scripts: dict | None = None,
    follow_refs: int = 1,
    visited: dict | None = None,
    prefab_cap: int = 400,
    minimal: bool = False,
    written: dict | None = None,
    exported_files: dict | None = None,
    game_refs: bool = False,
) -> CloneResult:
    out_folder = Path(out_folder)
    out_folder.mkdir(parents=True, exist_ok=True)
    root_name = context.game_object_names.get(root_key, "") or f"Object_{root_key[1]}"
    result = CloneResult(
        prefab_path=out_folder / f"{safe(root_name)}.prefab.json",
        folder=out_folder,
        root_name=root_name,
    )
    if exported_files is not None:
        result.exported_files = exported_files
    shared_written = written if written is not None else dict()

    order: list[ObjectKey] = []
    ordered_game_objects(context, root_key, order, set())
    paths = node_paths(context, root_key, order)
    component_owner: dict[ObjectKey, ObjectKey] = {}
    for game_object_key in order:
        record = context.game_objects.get(game_object_key)
        if record is None:
            continue
        for pointer in record.components:
            key = context.resolve(record.asset_key, pointer)
            if key is not None:
                component_owner[key] = game_object_key

    state = CloneState(
        context=context,
        out_folder=out_folder,
        result=result,
        paths=paths,
        component_owner=component_owner,
        written=shared_written,
        registry=managed_registry,
        scripts_index=mono_scripts or {},
        root_key=root_key,
        minimal=bool(minimal),
        game_refs=bool(game_refs),
        follow_budget=0 if minimal else ((1 << 30) if int(follow_refs) < 0 else max(0, int(follow_refs))),
        visited=visited if visited is not None else {root_key: result.prefab_path.name},
    )

    root_node = clone_node(state, root_key, include_scripts)
    document = {
        "format": "sunflowyr-new-prefab",
        "name": root_name,
        "root": root_node,
    }
    result.prefab_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    result.files.append(result.prefab_path)
    result.nodes = len(order)

    if visited is None and not minimal:
        pull_owning_definitions(state)

    depth = state.follow_budget
    while state.pending and depth > 0:
        batch = list(state.pending)
        state.pending.clear()
        depth -= 1
        for extra_root in batch:
            if len(result.extra_prefabs) >= prefab_cap:
                result.warnings.append(
                    f"stopped following references at {prefab_cap} prefabs, raise --prefab-cap for more"
                )
                state.pending.clear()
                break
            file_name = state.extra_prefabs.get(extra_root)
            if file_name is None:
                continue
            try:
                extra = clone_character_exact(
                    context,
                    extra_root,
                    out_folder,
                    include_scripts=include_scripts,
                    managed_registry=managed_registry,
                    mono_scripts=mono_scripts,
                    follow_refs=depth,
                    visited=state.visited,
                    prefab_cap=prefab_cap,
                    written=state.written,
                    exported_files=result.exported_files,
                    game_refs=state.game_refs,
                )
            except Exception as exc:
                result.warnings.append(f"referenced prefab {extra_root} couldnt be cloned: {exc}")
                continue
            produced = extra.prefab_path
            target = out_folder / file_name
            if produced != target:
                try:
                    if target.exists():
                        target.unlink()
                    produced.rename(target)
                except OSError:
                    pass
            result.extra_prefabs.append(file_name)
            result.files.extend(item for item in extra.files if item != produced)
            result.files.append(target)
            result.meshes += extra.meshes
            result.materials += extra.materials
            result.textures += extra.textures
            result.animations += extra.animations
            result.nodes += extra.nodes
            for script in extra.scripts:
                if script not in result.scripts:
                    result.scripts.append(script)
            result.warnings.extend(extra.warnings)
            result.external_refs.extend(extra.external_refs)
            for entry in extra.rebinds:
                if entry not in result.rebinds:
                    result.rebinds.append(entry)

    if visited is None and result.rebinds:
        rebind_path = out_folder / REBIND_SIDECAR_NAME
        rebind_path.write_text(
            json.dumps({"rebinds": result.rebinds}, ensure_ascii=False, indent=2) + chr(10),
            encoding="utf-8",
        )
        result.files.append(rebind_path)

    return result

@dataclass(slots=True)
class CloneState:
    context: ModelContextIndex
    out_folder: Path
    result: CloneResult
    paths: dict[ObjectKey, str]
    component_owner: dict[ObjectKey, ObjectKey]
    written: dict[ObjectKey, str]
    registry: Any | None
    scripts_index: dict
    root_key: ObjectKey
    follow_budget: int = 0
    extra_prefabs: dict = field(default_factory=dict)
    pending: list = field(default_factory=list)
    visited: dict = field(default_factory=dict)
    minimal: bool = False
    game_refs: bool = False
    mono_files: dict = field(default_factory=dict)
    mono_in_progress: set = field(default_factory=set)
    in_mono_asset: int = 0
    in_editable_asset: int = 0
    field_path: list = field(default_factory=list)
    current_script: str = ""
    current_asset: str = ""
    current_node: str = ""

OWNER_DEFINITION_CLASSES = {
    "PlayableTeamDefinition": "TeamRoster",
}

def pull_owning_definitions(state: CloneState) -> None:
    from .mono_export import read_monobehaviour_json

    owned = {
        key
        for key, file_name in state.written.items()
        if str(file_name).endswith(".mono.json")
    }
    if not owned:
        return
    context = state.context
    for asset_key, asset in context.assets.items():
        candidates = [obj for obj in asset.objects if obj.class_id == 114]
        if not candidates:
            continue
        with asset.path.open("rb") as handle:
            reader = EndianReader(handle, asset.endian)
            for obj in candidates:
                key = (asset_key, obj.path_id)
                if key in state.written:
                    continue
                try:
                    document, conversion = read_monobehaviour_json(
                        asset, reader, obj, state.scripts_index, state.registry
                    )
                except Exception:
                    continue
                if conversion.get("format") != "monobehaviour_json_typed_managed":
                    continue
                script = document.get("solarScript") or {}
                member = OWNER_DEFINITION_CLASSES.get(str(script.get("class_name") or ""))
                if member is None:
                    continue
                roster = document.get(member)
                if not isinstance(roster, list):
                    continue
                hit = False
                for entry in roster:
                    if not isinstance(entry, dict):
                        continue
                    target = decorated_target(entry)
                    if target is None:
                        pointer = as_pointer(entry)
                        if pointer is None:
                            continue
                        from .model_context import PPtr

                        target = context.resolve(asset_key, PPtr(pointer[0], pointer[1]))
                    if target is not None and target in owned:
                        hit = True
                        break
                if hit:
                    export_mono_asset(state, key, obj)

def ordered_game_objects(
    context: ModelContextIndex,
    game_object_key: ObjectKey,
    order: list[ObjectKey],
    seen: set[ObjectKey],
) -> None:
    if game_object_key in seen:
        return
    seen.add(game_object_key)
    order.append(game_object_key)
    for child in child_game_objects(context, game_object_key):
        ordered_game_objects(context, child, order, seen)

def child_game_objects(context: ModelContextIndex, game_object_key: ObjectKey) -> list[ObjectKey]:
    transform_key = context.transforms_by_game_object.get(game_object_key)
    transform = context.transforms.get(transform_key) if transform_key else None
    if transform is None:
        return []
    children: list[ObjectKey] = []
    for child in transform.children:
        child_key = context.resolve(transform.asset_key, child)
        child_transform = context.transforms.get(child_key) if child_key else None
        if child_transform is None:
            continue
        owner = context.resolve(child_transform.asset_key, child_transform.game_object)
        if owner is not None:
            children.append(owner)
    return children

def unique_file(result: CloneResult, key: ObjectKey, base: str, suffix: str) -> str:
    name = f"{base}{suffix}"
    owner = result.exported_files.get(name)
    if owner is None or owner == key:
        result.exported_files[name] = key
        return name
    name = f"{base}_{key[1]}{suffix}"
    result.exported_files[name] = key
    return name

def node_paths(
    context: ModelContextIndex,
    root_key: ObjectKey,
    order: list[ObjectKey],
) -> dict[ObjectKey, str]:
    paths: dict[ObjectKey, str] = {}
    root_name = context.game_object_names.get(root_key, "") or f"Object_{root_key[1]}"
    paths[root_key] = root_name

    def walk(key: ObjectKey) -> None:
        base = paths[key]
        used: dict[str, int] = {}
        for child in child_game_objects(context, key):
            if child in paths:
                continue
            name = context.game_object_names.get(child, "") or f"Object_{child[1]}"
            count = used.get(name, 0)
            used[name] = count + 1
            unique = name if count == 0 else f"{name} ({count})"
            paths[child] = f"{base}/{unique}"
            walk(child)

    walk(root_key)
    _ = order
    return paths

def clone_node(state: CloneState, game_object_key: ObjectKey, include_scripts: bool) -> dict[str, Any]:
    context = state.context
    record = context.game_objects.get(game_object_key)
    name = context.game_object_names.get(game_object_key, "") or f"Object_{game_object_key[1]}"
    path = state.paths.get(game_object_key, name)
    display_name = path.rsplit("/", 1)[-1]
    transform_key = context.transforms_by_game_object.get(game_object_key)
    transform = context.transforms.get(transform_key) if transform_key else None

    node: dict[str, Any] = {
        "name": name,
        "layer": int(record.layer) if record else 0,
        "tag": int(record.tag) if record else 0,
        "active": bool(record.active) if record else True,
    }
    if display_name != name:
        node["pathName"] = display_name
    if transform is not None:
        node["position"] = list(transform.local_position)
        node["rotation"] = list(transform.local_rotation)
        node["scale"] = list(transform.local_scale)
        rect = read_rect_transform(context, transform_key)
        if rect is not None:
            node["rectTransform"] = rect

    source = {"container": game_object_key[0], "gameObjectPathId": game_object_key[1]}
    if transform_key is not None:
        source["transformPathId"] = transform_key[1]
    node["source"] = source

    components: list[dict[str, Any]] = []
    if record is not None:
        for pointer in record.components:
            key = context.resolve(record.asset_key, pointer)
            obj = context.objects.get(key) if key else None
            if obj is None or obj.class_id in SKIP_COMPONENT_CLASSES:
                continue
            spec = clone_component(state, key, obj, include_scripts)
            if spec is not None:
                components.append(spec)
    node["components"] = components

    children = [
        clone_node(state, child, include_scripts)
        for child in child_game_objects(context, game_object_key)
    ]
    node["children"] = children
    return node

def read_rect_transform(context: ModelContextIndex, transform_key: ObjectKey | None) -> dict[str, Any] | None:
    if transform_key is None:
        return None
    obj = context.objects.get(transform_key)
    if obj is None or obj.class_id != 224:
        return None
    asset = context.assets.get(transform_key[0])
    if asset is None:
        return None
    try:
        from .ui_export import read_ui_component_json

        with asset.path.open("rb") as handle:
            document, conversion = read_ui_component_json(
                asset, EndianReader(handle, asset.endian), obj
            )
    except Exception:
        return None
    return {
        "anchorMin": document.get("m_AnchorMin") or [0.0, 0.0],
        "anchorMax": document.get("m_AnchorMax") or [1.0, 1.0],
        "anchoredPosition": document.get("m_AnchoredPosition") or [0.0, 0.0],
        "sizeDelta": document.get("m_SizeDelta") or [0.0, 0.0],
        "pivot": document.get("m_Pivot") or [0.5, 0.5],
    }

def clone_component(
    state: CloneState,
    key: ObjectKey,
    obj: Any,
    include_scripts: bool,
) -> dict[str, Any] | None:
    context = state.context
    asset = context.assets.get(key[0])
    if asset is None:
        return None
    kind = class_name(obj.class_id)
    if obj.class_id == 114:
        if not include_scripts:
            return None
        return clone_mono_behaviour(state, key, obj, asset)
    from .map_component_export import MapComponentExportError, parse_map_component_fields
    from .ui_export import UI_CLASS_KINDS, read_ui_component_json

    try:
        if obj.class_id in UI_CLASS_KINDS:
            with asset.path.open("rb") as handle:
                document, conversion = read_ui_component_json(
                    asset, EndianReader(handle, asset.endian), obj
                )
            fields = {k: v for k, v in document.items() if not k.startswith("solar")}
        else:
            fields = parse_map_component_fields(asset, obj, context)
    except (MapComponentExportError, Exception):
        state.result.warnings.append(f"{kind} on {state.paths.get(state.component_owner.get(key, key), '?')} couldnt be read")
        return None
    previous = (state.current_script, state.current_node, state.current_asset)
    state.current_script = kind
    state.current_node = state.paths.get(state.component_owner.get(key, key), "")
    state.current_asset = state.result.prefab_path.name
    try:
        rewritten = rewrite_value(state, asset.relative_path.replace("\\", "/").lower(), fields)
    finally:
        state.current_script, state.current_node, state.current_asset = previous
    rewritten.pop("m_GameObject", None)
    return {"type": kind, "fields": rewritten}

def clone_mono_behaviour(
    state: CloneState,
    key: ObjectKey,
    obj: Any,
    asset: SerializedAsset,
) -> dict[str, Any] | None:
    from .mono_export import read_monobehaviour_json

    try:
        with asset.path.open("rb") as handle:
            reader = EndianReader(handle, asset.endian)
            document, conversion = read_monobehaviour_json(
                asset, reader, obj, state.scripts_index, state.registry
            )
    except Exception:
        return None
    script = document.get("solarScript") or {}
    class_text = str(script.get("class_name") or "")
    if not class_text:
        return None
    spec: dict[str, Any] = {
        "type": "MonoBehaviour",
        "script": {
            "assembly": str(script.get("assembly_name") or "Assembly-CSharp"),
            "namespace": str(script.get("namespace") or ""),
            "class": class_text,
        },
    }
    enabled = document.get("m_Enabled")
    if enabled is not None:
        spec["enabled"] = bool(enabled)
    if class_text not in state.result.scripts:
        state.result.scripts.append(class_text)
    if conversion.get("format") != "monobehaviour_json_typed_managed":
        return spec
    asset_key = asset.relative_path.replace("\\", "/").lower()
    fields = {
        name: value
        for name, value in document.items()
        if name not in {"m_GameObject", "m_Enabled", "m_Script", "m_Name", "solarScript", "solarRawSerializedFields"}
    }
    previous = (state.current_script, state.current_node, state.current_asset)
    state.current_script = class_text
    state.current_node = state.paths.get(state.component_owner.get(key, key), "")
    state.current_asset = state.result.prefab_path.name
    try:
        spec["fields"] = rewrite_value(state, asset_key, fields)
    finally:
        state.current_script, state.current_node, state.current_asset = previous
    return spec

def rewrite_mono_fields(state: CloneState, asset_key: str, fields: Any) -> Any:
    state.in_mono_asset += 1
    try:
        return rewrite_value(state, asset_key, fields)
    finally:
        state.in_mono_asset -= 1

def rewrite_value(state: CloneState, asset_key: str, value: Any) -> Any:
    from .model_context import PPtr

    if isinstance(value, dict):
        pointer = as_pointer(value)
        if pointer is not None:
            target = decorated_target(value)
            if target is None:
                target = state.context.resolve(asset_key, PPtr(pointer[0], pointer[1]))
            rewritten = rewrite_pointer(state, target)
            if rewritten is None and state.in_editable_asset:
                return {"m_FileID": 0, "m_PathID": 0}
            return rewritten
        rewritten: dict[str, Any] = {}
        for name, item in value.items():
            state.field_path.append(str(name))
            try:
                rewritten[name] = rewrite_value(state, asset_key, item)
            finally:
                state.field_path.pop()
        return rewritten
    if isinstance(value, list):
        return [rewrite_value(state, asset_key, item) for item in value]
    return value

def as_pointer(value: dict[str, Any]) -> tuple[int, int] | None:
    keys = set(value.keys())
    for file_key, path_key in (("m_FileID", "m_PathID"), ("fileId", "pathId")):
        if file_key in keys and path_key in keys:
            try:
                return int(value[file_key]), int(value[path_key])
            except (TypeError, ValueError):
                return None
    return None

def decorated_target(value: dict[str, Any]) -> ObjectKey | None:
    container = value.get("container")
    path_id = value.get("resolvedPathId")
    if isinstance(container, str) and container and isinstance(path_id, int):
        return (container.replace("\\", "/").lower(), int(path_id))
    return None

def rewrite_pointer(state: CloneState, target: ObjectKey | None) -> Any:
    if target is None:
        return None
    context = state.context
    if target in state.paths:
        if state.in_mono_asset:
            if target == state.root_key:
                return {"asset": state.result.prefab_path.name}
            return {
                "prefab": state.result.prefab_path.name,
                "node": state.paths[target],
                "gameObject": True,
            }
        return {"node": state.paths[target], "gameObject": True}
    owner = state.component_owner.get(target)
    if owner is not None and owner in state.paths:
        obj = context.objects.get(target)
        if state.in_mono_asset:
            reference: dict[str, Any] = {
                "prefab": state.result.prefab_path.name,
                "node": state.paths[owner],
            }
            if obj is not None and obj.class_id == 114:
                script = state.scripts_index.get(script_key(context, target))
                class_text = getattr(script, "class_name", "") if script else ""
                if not class_text:
                    return None
                reference["component"] = class_text
            elif obj is not None:
                reference["component"] = class_name(obj.class_id)
            return reference
        if obj is not None:
            if obj.class_id in SKIP_COMPONENT_CLASSES:
                return {"node": state.paths[owner]}
            if obj.class_id == 114:
                script = state.scripts_index.get(script_key(context, target))
                class_text = getattr(script, "class_name", "") if script else ""
                if not class_text:
                    state.result.warnings.append(
                        f"component reference {target} has no resolvable script class"
                    )
                    return None
                return {"node": state.paths[owner], "component": class_text}
            return {"node": state.paths[owner], "component": class_name(obj.class_id)}
    transform = context.transforms.get(target)
    if transform is not None:
        owner_key = context.resolve(transform.asset_key, transform.game_object)
        if owner_key in state.paths:
            if state.in_mono_asset:
                return {
                    "prefab": state.result.prefab_path.name,
                    "node": state.paths[owner_key],
                }
            return {"node": state.paths[owner_key]}
    obj = context.objects.get(target)
    if obj is not None and obj.class_id in EXPORTABLE_ASSET_CLASSES:
        exported = export_asset(state, target, obj)
        if exported is not None:
            return {"asset": exported}
    owner_root = owning_root(context, target) if obj is not None else None
    if obj is not None and obj.class_id == 114 and owner_root is None and not state.minimal:
        exported = export_mono_asset(state, target, obj)
        if exported is not None:
            return {"asset": exported}
    if obj is not None and state.follow_budget > 0:
        if owner_root is not None and owner_root != state.root_key:
            file_name = schedule_prefab(state, owner_root)
            if file_name is not None:
                if target == owner_root:
                    return {"asset": file_name}
                cross = cross_prefab_reference(state, file_name, owner_root, target, obj)
                if cross is not None:
                    return cross
                if state.game_refs:
                    return game_reference(state, target)
                state.result.external_refs.append(
                    f"{class_name(obj.class_id)} inside cloned prefab '{file_name}' "
                    f"({target[0]} path {target[1]})"
                )
                return None
    if obj is not None:
        if state.game_refs:
            return game_reference(state, target)
        state.result.external_refs.append(f"{class_name(obj.class_id)} {target[0]} path {target[1]}")
        record_rebind(state, target, obj)
    return None

def game_reference(state: CloneState, target: ObjectKey) -> dict[str, Any]:
    name = state.context.object_names.get(target) or state.context.game_object_names.get(target) or "object"
    return {"game": str(name), "in": str(target[0]), "id": int(target[1])}

REBINDABLE_CLASSES = {
    243: "UnityEngine.Audio.AudioMixerGroup",
    91: "UnityEngine.RuntimeAnimatorController",
}

def record_rebind(state: CloneState, target: ObjectKey, obj: Any) -> None:
    runtime_type = REBINDABLE_CLASSES.get(obj.class_id)
    if runtime_type is None or not state.field_path:
        return
    name = state.context.object_names.get(target) or ""
    if not name:
        return
    entry = {
        "asset": state.current_asset,
        "node": state.current_node,
        "script": state.current_script,
        "memberPath": ".".join(state.field_path),
        "type": runtime_type,
        "name": name,
    }
    if entry not in state.result.rebinds:
        state.result.rebinds.append(entry)

def export_mono_asset(state: CloneState, key: ObjectKey, obj: Any) -> str | None:
    existing = state.written.get(key)
    if existing is not None:
        return existing
    if key in state.mono_in_progress:
        return None
    context = state.context
    asset = context.assets.get(key[0])
    if asset is None:
        return None
    from .mono_export import read_monobehaviour_json

    state.mono_in_progress.add(key)
    try:
        with asset.path.open("rb") as handle:
            document, conversion = read_monobehaviour_json(
                asset, EndianReader(handle, asset.endian), obj, state.scripts_index, state.registry
            )
    except Exception:
        state.mono_in_progress.discard(key)
        return None
    script = document.get("solarScript") or {}
    class_text = str(script.get("class_name") or "")
    if not class_text or conversion.get("format") != "monobehaviour_json_typed_managed":
        state.mono_in_progress.discard(key)
        return None
    name = str(document.get("m_Name") or "") or f"{class_text}_{key[1]}"
    file_name = f"{safe(name)}.mono.json"
    if file_name in state.mono_files and state.mono_files[file_name] != key:
        file_name = f"{safe(name)}_{key[1]}.mono.json"
    state.mono_files[file_name] = key
    state.written[key] = file_name
    asset_key = asset.relative_path.replace("\\", "/").lower()
    fields = {
        field_name: value
        for field_name, value in document.items()
        if field_name
        not in {"m_GameObject", "m_Enabled", "m_Script", "m_Name", "solarScript", "solarRawSerializedFields"}
    }
    previous = (state.current_script, state.current_node, state.current_asset)
    state.current_script = class_text
    state.current_node = ""
    state.current_asset = file_name
    outer_field_path = state.field_path
    state.field_path = []
    try:
        rewritten_fields = rewrite_mono_fields(state, asset_key, fields)
    finally:
        state.field_path = outer_field_path
        state.current_script, state.current_node, state.current_asset = previous
    payload = {
        "format": "sunflowyr-new-monobehaviour",
        "name": name,
        "script": {
            "assembly": str(script.get("assembly_name") or "Assembly-CSharp"),
            "namespace": str(script.get("namespace") or ""),
            "class": class_text,
        },
        "fields": rewritten_fields,
    }
    path = state.out_folder / file_name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
    state.result.files.append(path)
    state.result.data_assets += 1
    if class_text not in state.result.scripts:
        state.result.scripts.append(class_text)
    state.mono_in_progress.discard(key)
    return file_name

def cross_prefab_reference(
    state: CloneState,
    prefab_file: str,
    owner_root: ObjectKey,
    target: ObjectKey,
    obj: Any,
) -> dict[str, Any] | None:
    context = state.context
    owner = state.component_owner.get(target)
    if owner is None:
        owner = component_owner_lookup(context, target)
    if owner is None:
        return None
    node_path = relative_node_path(context, owner_root, owner)
    if node_path is None:
        return None
    reference: dict[str, Any] = {"prefab": prefab_file, "node": node_path}
    if obj.class_id == 114:
        script = state.scripts_index.get(script_key(context, target))
        class_text = getattr(script, "class_name", "") if script else ""
        if not class_text:
            return None
        reference["component"] = class_text
    elif obj.class_id != 1:
        reference["component"] = class_name(obj.class_id)
    else:
        reference["gameObject"] = True
    return reference

def script_key(context: ModelContextIndex, mono_key: ObjectKey) -> ObjectKey | None:
    from .model_context import PPtr

    asset = context.assets.get(mono_key[0])
    obj = context.objects.get(mono_key)
    if asset is None or obj is None:
        return None
    try:
        with asset.path.open("rb") as handle:
            reader = EndianReader(handle, asset.endian)
            reader.seek(obj.byte_start)
            reader.i32()
            reader.i64()
            reader.u8()
            reader.align()
            return context.resolve(mono_key[0], PPtr(reader.i32(), reader.i64()))
    except Exception:
        return None

OWNER_INDEX_ATTRIBUTE = "sunflowyr_component_owner_index"

def component_owner_index(context: ModelContextIndex) -> dict[ObjectKey, ObjectKey]:
    index = getattr(context, OWNER_INDEX_ATTRIBUTE, None)
    if index is None:
        index = {}
        for game_object_key, record in context.game_objects.items():
            for pointer in record.components:
                component_key = context.resolve(record.asset_key, pointer)
                if component_key is not None:
                    index[component_key] = game_object_key
        try:
            setattr(context, OWNER_INDEX_ATTRIBUTE, index)
        except AttributeError:
            pass
    return index

def component_owner_lookup(context: ModelContextIndex, key: ObjectKey) -> ObjectKey | None:
    return component_owner_index(context).get(key)

def relative_node_path(
    context: ModelContextIndex,
    root_key: ObjectKey,
    game_object_key: ObjectKey,
) -> str | None:
    names: list[str] = []
    current = game_object_key
    seen: set[ObjectKey] = set()
    while current is not None and current not in seen:
        seen.add(current)
        names.append(context.game_object_names.get(current, "") or f"Object_{current[1]}")
        if current == root_key:
            return "/".join(reversed(names))
        transform_key = context.transforms_by_game_object.get(current)
        transform = context.transforms.get(transform_key) if transform_key else None
        if transform is None:
            return None
        parent = context.resolve(transform.asset_key, transform.father)
        parent_transform = context.transforms.get(parent) if parent else None
        if parent_transform is None:
            return None
        current = context.resolve(parent_transform.asset_key, parent_transform.game_object)
    return None

def owning_root(context: ModelContextIndex, key: ObjectKey) -> ObjectKey | None:
    obj = context.objects.get(key)
    if obj is None:
        return None
    game_object_key: ObjectKey | None = None
    if obj.class_id == 1:
        game_object_key = key
    else:
        transform = context.transforms.get(key)
        if transform is not None:
            game_object_key = context.resolve(transform.asset_key, transform.game_object)
        else:
            game_object_key = component_owner_index(context).get(key)
    if game_object_key is None:
        return None
    transform_key = context.transforms_by_game_object.get(game_object_key)
    root_transform = find_root_transform(context, transform_key) if transform_key else None
    if root_transform is None:
        return game_object_key
    transform = context.transforms.get(root_transform)
    if transform is None:
        return game_object_key
    return context.resolve(transform.asset_key, transform.game_object) or game_object_key

def schedule_prefab(state: CloneState, root_key: ObjectKey) -> str | None:
    existing = state.extra_prefabs.get(root_key)
    if existing is not None:
        return existing
    already = state.visited.get(root_key)
    if already is not None:
        return already
    name = state.context.game_object_names.get(root_key, "") or f"Object_{root_key[1]}"
    file_name = f"{safe(name)}.prefab.json"
    if file_name == state.result.prefab_path.name:
        file_name = f"{safe(name)}_{root_key[1]}.prefab.json"
    state.visited[root_key] = file_name
    state.extra_prefabs[root_key] = file_name
    state.pending.append(root_key)
    return file_name

def export_animation(
    context: ModelContextIndex,
    key: ObjectKey,
    out_folder: Path,
    result: CloneResult,
    state: CloneState | None = None,
) -> str | None:
    from .animation_package import AnimationPackageError, export_animation_package
    from .model_context import PPtr

    asset = context.assets.get(key[0])
    obj = context.objects.get(key)
    if asset is None or obj is None:
        return None
    name = context.object_names.get(key) or f"Clip_{key[1]}"
    file_name = unique_file(result, key, safe(name), ".sanim")
    path = out_folder / file_name

    def pointers(file_id: int, path_id: int) -> dict[str, Any] | None:
        if state is None:
            return None
        state.written.setdefault(key, file_name)
        target = context.resolve(key[0], PPtr(file_id, path_id))
        rewritten = rewrite_pointer(state, target)
        if isinstance(rewritten, dict) and isinstance(rewritten.get("asset"), str):
            return {"asset": rewritten["asset"]}
        result.warnings.append(f'animation "{name}" points at {target}, which cant be carried into a new asset, so that pointer was cleared')
        return None

    try:
        export_animation_package(asset, obj, context, path, {}, pointers)
    except (AnimationPackageError, Exception) as exc:
        if state is not None and state.written.get(key) == file_name:
            state.written.pop(key)
        result.warnings.append(f'couldnt export animation "{name}": {exc}')
        return None
    result.files.append(path)
    result.animations += 1
    return file_name

def export_audio(
    context: ModelContextIndex,
    key: ObjectKey,
    out_folder: Path,
    result: CloneResult,
) -> str | None:
    from .audio_convert import AudioDecodeError, audio_view_extension, save_audio_view

    asset = context.assets.get(key[0])
    obj = context.objects.get(key)
    if asset is None or obj is None:
        return None
    name = context.object_names.get(key) or f"Audio_{key[1]}"
    try:
        with asset.path.open("rb") as handle:
            description = describe_object(asset, EndianReader(handle, asset.endian), obj)
        payload = next((item for item in description.payloads if item.kind == "audio_clip"), None)
        if payload is None:
            return None
        source = resolve_resource(asset, payload)
        if source is None:
            return None
        data = b"".join(read_range(source, payload.source_offset, payload.source_size))
        extension = audio_view_extension(data, payload.details)
        file_name = unique_file(result, key, safe(name), extension)
        path = out_folder / file_name
        save_audio_view(data, payload.details, path)
    except (AudioDecodeError, KeyError, OSError, ValueError) as exc:
        result.warnings.append(f'could not export audio "{name}": {exc}')
        return None
    result.files.append(path)
    result.audio += 1
    return file_name

def export_physic_material(
    context: ModelContextIndex,
    key: ObjectKey,
    out_folder: Path,
    result: CloneResult,
) -> str | None:
    from .map_component_export import parse_map_component_fields

    asset = context.assets.get(key[0])
    obj = context.objects.get(key)
    if asset is None or obj is None:
        return None
    try:
        fields = parse_map_component_fields(asset, obj, context)
    except Exception:
        return None
    name = str(fields.get("m_Name") or f"PhysicMaterial_{key[1]}")
    document = {
        "format": "sunflowyr-new-physic-material",
        "name": name,
        "fields": {
            "m_Name": name,
            "dynamicFriction": float(fields.get("dynamicFriction", 0.6)),
            "staticFriction": float(fields.get("staticFriction", 0.6)),
            "bounciness": float(fields.get("bounciness", 0.0)),
            "frictionCombine": int(fields.get("frictionCombine", 0)),
            "bounceCombine": int(fields.get("bounceCombine", 0)),
        },
    }
    file_name = unique_file(result, key, safe(name), ".physicmaterial.json")
    path = out_folder / file_name
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
    result.files.append(path)
    return file_name

def export_asset(state: CloneState, key: ObjectKey, obj: Any) -> str | None:
    existing = state.written.get(key)
    if existing is not None:
        return existing
    if obj.class_id == 43:
        name = export_mesh(state.context, key, state.out_folder, state.result)
    elif obj.class_id in (28, 89):
        name = export_texture(state.context, key, state.out_folder, state.result)
    elif obj.class_id == 21:
        name = export_material(state.context, key, state.out_folder, state.result, state.written)
    elif obj.class_id == 74:
        name = export_animation(state.context, key, state.out_folder, state.result, state)
    elif obj.class_id == 83:
        name = export_audio(state.context, key, state.out_folder, state.result)
    elif obj.class_id == 134:
        name = export_physic_material(state.context, key, state.out_folder, state.result)
    elif obj.class_id == 213:
        name = export_sprite(
            state.context, key, state.out_folder, state.result, state.written
        )
    elif obj.class_id == 128:
        name = export_font(state.context, key, state.out_folder, state.result)
    elif obj.class_id in EDITABLE_BUNDLE_CLASSES:
        name = export_editable_asset(state, key, obj)
    else:
        name = None
    if name is not None:
        state.written[key] = name
    return name

def export_mesh(
    context: ModelContextIndex,
    mesh_key: ObjectKey,
    out_folder: Path,
    result: CloneResult,
) -> str | None:
    asset = context.assets.get(mesh_key[0])
    obj = context.objects.get(mesh_key)
    if asset is None or obj is None:
        result.warnings.append(f"mesh {mesh_key} is missing from the game data")
        return None
    name = context.object_names.get(mesh_key) or f"Mesh_{mesh_key[1]}"
    file_name = unique_file(result, mesh_key, safe(name), ".glb")
    path = out_folder / file_name
    try:
        export_mesh_glb(asset, obj, path, context)
    except Exception as exc:
        result.warnings.append(f'couldnt export mesh "{name}": {exc}')
        return None
    result.files.append(path)
    result.meshes += 1
    return file_name

def export_material(
    context: ModelContextIndex,
    material_key: ObjectKey,
    out_folder: Path,
    result: CloneResult,
    written_assets: dict[ObjectKey, str],
) -> str | None:
    record = context.materials.get(material_key)
    if record is None:
        result.warnings.append(f"material {material_key} couldnt be read")
        return None
    textures: dict[str, Any] = {}
    for slot, texture_record in record.textures.items():
        texture_key = context.resolve(record.asset_key, texture_record.texture)
        if texture_key is None:
            continue
        source = context.objects.get(texture_key)
        if source is not None and source.class_id == 89:
            texture_file = written_assets.get(texture_key)
            if texture_file is None:
                texture_file = export_cubemap(context, texture_key, out_folder, result)
                if texture_file is None:
                    result.warnings.append(
                        f'material "{record.name}" slot {slot} cubemap couldnt be copied'
                    )
                    continue
                written_assets[texture_key] = texture_file
            textures[slot] = {
                "file": texture_file,
                "scale": list(texture_record.scale),
                "offset": list(texture_record.offset),
            }
            continue
        texture_file = written_assets.get(texture_key)
        if texture_file is None:
            texture_file = export_texture(context, texture_key, out_folder, result)
            if texture_file is None:
                continue
            written_assets[texture_key] = texture_file
        textures[slot] = {
            "file": texture_file,
            "scale": list(texture_record.scale),
            "offset": list(texture_record.offset),
        }
    shader_key = resolve_shader_key(context, record)
    shader_file, declared = export_shader_by_key(context, shader_key, out_folder, result)
    shader_name = declared or resolve_shader_name(context, record) or "Standard"
    document = {
        "format": "sunflowyr-new-material",
        "name": record.name or f"Material_{material_key[1]}",
        "shaderName": shader_name,
        "shaderAsset": shader_file,
        "shaderKeywords": record.shader_keywords or "",
        "customRenderQueue": int(record.custom_render_queue),
        "textures": textures,
        "floats": {key: float(value) for key, value in record.floats.items()},
        "colors": {key: list(value) for key, value in record.colors.items()},
    }
    file_name = unique_file(result, material_key, safe(document["name"]), ".material.json")
    path = out_folder / file_name
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result.files.append(path)
    result.materials += 1
    return file_name

def export_shader_by_key(
    context: ModelContextIndex,
    shader_key: ObjectKey | None,
    out_folder: Path,
    result: CloneResult,
) -> tuple[str | None, str]:
    from .asset_bundle_shader import declared_shader_name, export_shader_closure
    from .shader_export import read_shader_object

    if shader_key is None:
        return None, ""
    asset = context.assets.get(shader_key[0])
    obj = context.objects.get(shader_key)
    if asset is None or obj is None or not asset.data_root:
        return None, ""
    data_root = Path(asset.data_root)
    try:
        declared = declared_shader_name(read_shader_object(asset, obj).script)
    except Exception:
        declared = ""
    try:
        file_name = export_shader_closure(
            data_root, asset.relative_path.replace("\\", "/"), shader_key[1], out_folder
        )
    except Exception as exc:
        result.warnings.append(f'shader for {shader_key} couldnt be copied: {exc}')
        return None, declared
    if file_name is None:
        result.warnings.append(
            f'shader "{declared or shader_key}" couldnt be copied; '
            "the plugin assigns it by name"
        )
        return None, declared
    result.shaders += 1
    return file_name, declared

def resolve_shader_key(context: ModelContextIndex, record: Any) -> ObjectKey | None:
    from .model_context import PPtr

    asset = context.assets.get(record.asset_key)
    obj = context.objects.get(record.key)
    if asset is None or obj is None:
        return None
    try:
        with asset.path.open("rb") as handle:
            reader = EndianReader(handle, asset.endian)
            reader.seek(obj.byte_start)
            if asset.platform == -2:
                reader.u32()
                reader.i32()
                reader.i64()
                reader.i32()
                reader.i64()
            reader.aligned_string(max_length=4096)
            pointer = PPtr(reader.i32(), reader.i64())
        return context.resolve(record.asset_key, pointer)
    except Exception:
        return None

def resolve_shader_name(context: ModelContextIndex, record: Any) -> str:
    from .model_context import PPtr

    asset = context.assets.get(record.asset_key)
    obj = context.objects.get(record.key)
    if asset is None or obj is None:
        return ""
    try:
        with asset.path.open("rb") as handle:
            reader = EndianReader(handle, asset.endian)
            reader.seek(obj.byte_start)
            if asset.platform == -2:
                reader.u32()
                reader.i32()
                reader.i64()
                reader.i32()
                reader.i64()
            reader.aligned_string(max_length=4096)
            shader = PPtr(reader.i32(), reader.i64())
        shader_key = context.resolve(record.asset_key, shader)
        if shader_key is None:
            return ""
        name = context.object_names.get(shader_key)
        if name:
            return name
        shader_asset = context.assets.get(shader_key[0])
        shader_obj = context.objects.get(shader_key)
        if shader_asset is None or shader_obj is None:
            return ""
        with shader_asset.path.open("rb") as handle:
            reader = EndianReader(handle, shader_asset.endian)
            return describe_object(shader_asset, reader, shader_obj).name or ""
    except Exception:
        return ""

def export_font(
    context: ModelContextIndex,
    font_key: ObjectKey,
    out_folder: Path,
    result: CloneResult,
) -> str | None:
    asset = context.assets.get(font_key[0])
    obj = context.objects.get(font_key)
    if asset is None or obj is None:
        return None
    name = context.object_names.get(font_key) or f"Font_{font_key[1]}"
    try:
        with asset.path.open("rb") as handle:
            _, _, font_bytes = read_font_json(asset, EndianReader(handle, asset.endian), obj)
    except Exception as exc:
        result.warnings.append(f'font "{name}" couldnt be read: {exc}')
        return None
    if not font_bytes:
        result.warnings.append(f'font "{name}" has no embedded font data (dynamic font)')
        return None
    file_name = unique_file(result, font_key, safe(name), font_extension(font_bytes))
    path = out_folder / file_name
    path.write_bytes(font_bytes)
    result.files.append(path)
    result.fonts += 1
    return file_name

def export_editable_asset(state: CloneState, key: ObjectKey, obj: Any) -> str | None:
    from .editable_object_export import EDITABLE_ASSET_SCHEMAS
    from .unity_typetree_codec import load_schema, read_typetree

    definition = EDITABLE_ASSET_SCHEMAS.get(obj.class_id)
    if definition is None:
        return None
    context = state.context
    asset = context.assets.get(key[0])
    if asset is None:
        return None
    name = context.object_names.get(key) or f"{class_name(obj.class_id)}_{key[1]}"
    try:
        with asset.path.open("rb") as handle:
            handle.seek(obj.byte_start)
            data = handle.read(obj.byte_size)
        fields = read_typetree(data, load_schema(definition[1]), asset.endian)
        decorate_pointers(fields, asset, context)
    except Exception as exc:
        state.result.warnings.append(f'{class_name(obj.class_id)} "{name}" couldnt be read: {exc}')
        return None

    asset_key = asset.relative_path.replace("\\", "/").lower()
    previous = (state.current_script, state.current_node, state.current_asset)
    outer_field_path = state.field_path
    state.field_path = []
    file_name = unique_file(state.result, key, safe(str(fields.get("m_Name") or name)), ".editable.json")
    state.written[key] = file_name
    state.in_editable_asset += 1
    state.current_script = class_name(obj.class_id)
    state.current_node = ""
    state.current_asset = file_name
    try:
        rewritten = rewrite_value(state, asset_key, fields)
    finally:
        state.in_editable_asset -= 1
        state.field_path = outer_field_path
        state.current_script, state.current_node, state.current_asset = previous

    document = {
        "format": "sunflowyr-editable-asset",
        "name": str(fields.get("m_Name") or name),
        "classId": obj.class_id,
        "className": class_name(obj.class_id),
        "schema": definition[1],
        "fields": rewritten,
    }
    path = state.out_folder / file_name
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
    state.result.files.append(path)
    state.result.editable_assets += 1
    return file_name

def export_cubemap(
    context: ModelContextIndex,
    cubemap_key: ObjectKey,
    out_folder: Path,
    result: CloneResult,
) -> str | None:
    from .asset_bundle_cubemap import export_game_cubemap

    asset = context.assets.get(cubemap_key[0])
    if asset is None or not asset.data_root:
        return None
    file_name = export_game_cubemap(
        Path(asset.data_root),
        asset.relative_path.replace("\\", "/"),
        cubemap_key[1],
        out_folder,
    )
    if file_name is None:
        return None
    result.textures += 1
    return file_name

def export_sprite(
    context: ModelContextIndex,
    sprite_key: ObjectKey,
    out_folder: Path,
    result: CloneResult,
    written: dict[ObjectKey, str],
) -> str | None:
    asset = context.assets.get(sprite_key[0])
    obj = context.objects.get(sprite_key)
    if asset is None or obj is None:
        return None
    name = context.object_names.get(sprite_key) or f"Sprite_{sprite_key[1]}"
    try:
        with asset.path.open("rb") as handle:
            sprite = read_sprite(asset, EndianReader(handle, asset.endian), obj)
    except Exception as exc:
        result.warnings.append(f'sprite "{name}" couldnt be read: {exc}')
        return None

    texture_key = context.resolve(sprite_key[0], sprite.render_data.texture)
    if texture_key is None:
        result.warnings.append(f'sprite "{name}" has no resolvable texture')
        return None
    texture_file = written.get(texture_key)
    if texture_file is None:
        texture_file = export_texture(context, texture_key, out_folder, result)
        if texture_file is None:
            return None
        written[texture_key] = texture_file

    rect = sprite.rect
    document = {
        "format": "sunflowyr-new-sprite",
        "name": sprite.name or name,
        "texture": texture_file,
        "rect": [
            float(rect.get("x", 0.0)),
            float(rect.get("y", 0.0)),
            float(rect.get("width", 0.0)),
            float(rect.get("height", 0.0)),
        ],
        "pivot": list(sprite.pivot),
        "border": list(sprite.border),
        "pixelsToUnits": float(sprite.pixels_to_units) or 100.0,
        "extrude": int(sprite.extrude),
        "isPolygon": bool(sprite.is_polygon),
    }
    file_name = unique_file(result, sprite_key, safe(document["name"]), ".sprite.json")
    path = out_folder / file_name
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result.files.append(path)
    result.sprites += 1
    return file_name

def export_texture(
    context: ModelContextIndex,
    texture_key: ObjectKey,
    out_folder: Path,
    result: CloneResult,
) -> str | None:
    asset = context.assets.get(texture_key[0])
    obj = context.objects.get(texture_key)
    if asset is None or obj is None:
        return None
    name = context.texture_names.get(texture_key) or context.object_names.get(texture_key) or f"Texture_{texture_key[1]}"
    file_name = unique_file(result, texture_key, safe(name), ".png")
    path = out_folder / file_name
    try:
        with asset.path.open("rb") as handle:
            reader = EndianReader(handle, asset.endian)
            description = describe_object(asset, reader, obj)
        payload = next(
            (item for item in description.payloads if item.kind in {"texture2d", "cubemap"}),
            None,
        )
        if payload is None:
            result.warnings.append(f'texture "{name}" has no image payload')
            return None
        source = resolve_resource(asset, payload)
        if source is None:
            result.warnings.append(f'texture "{name}" data file is missing')
            return None
        data = b"".join(read_range(source, payload.source_offset, payload.source_size))
        save_png(
            data,
            int(payload.details["width"]),
            int(payload.details["height"]),
            int(payload.details["texture_format"]),
            path,
            flip_vertical=payload.kind != "cubemap",
            image_count=int(payload.details.get("image_count", 1)),
            complete_image_size=int(payload.details.get("complete_image_size", 0)),
        )
    except (TextureDecodeError, KeyError, OSError, ValueError) as exc:
        result.warnings.append(f'could not export texture "{name}": {exc}')
        return None
    result.files.append(path)
    result.textures += 1
    return file_name

def resolve_resource(asset: SerializedAsset, payload: Any) -> Path | None:
    if payload.source_path == asset.relative_path:
        return asset.path
    candidate = asset.path.parent / payload.source_path
    if candidate.exists():
        return candidate
    matches = list(asset.path.parent.rglob(Path(payload.source_path).name))
    return matches[0] if matches else None

def safe(value: str, limit: int = 80) -> str:
    cleaned = "".join(
        char if char.isalnum() or char in " ._-()" else "_" for char in str(value)
    ).strip(" .")
    return (cleaned or "asset")[:limit]


def describe_clone(result: CloneResult) -> str:
    parts = [
        f"{result.root_name}: {result.meshes} mesh(es), {result.materials} materials, "
        f"{result.textures} textures, {result.animations} animations, "
        f"{result.data_assets} data assets, {result.audio} audio"
    ]
    if result.scripts:
        parts.append("scripts: " + ", ".join(result.scripts))
    if result.nodes:
        parts.insert(0, f"{result.nodes} node(s)")
    if result.extra_prefabs:
        parts.append(f"{len(result.extra_prefabs)} referenced prefabs pulled in")
    if result.external_refs:
        parts.append(f"{len(result.external_refs)} references outside the bundle")
    if result.warnings:
        parts.append(f"{len(result.warnings)} warnings")
    return " | ".join(parts)

CLONE_REPORT_NAME = "sunflowyr_clone_report.log"

def write_clone_report(result: CloneResult, path: Path | None = None) -> Path:
    path = Path(path) if path is not None else result.folder / CLONE_REPORT_NAME
    lines: list[str] = []
    add = lines.append

    add("Sunflowyr clone report")
    add("=" * 72)
    add(f"character      : {result.root_name}")
    add(f"output folder  : {result.folder}")
    add(f"prefab         : {result.prefab_path.name}")
    add(f"files written  : {len(result.files)}")
    add("")
    add(f"nodes          : {result.nodes}")
    add(f"meshes         : {result.meshes}")
    add(f"materials      : {result.materials}")
    add(f"textures       : {result.textures}")
    add(f"sprites        : {result.sprites}")
    add(f"fonts          : {result.fonts}")
    add(f"animations     : {result.animations}")
    add(f"data assets    : {result.data_assets}")
    add(f"audio          : {result.audio}")
    add(f"extra prefabs  : {len(result.extra_prefabs)}")
    add("")
    add(f"warnings       : {len(result.warnings)}")
    add(f"nulled refs    : {len(result.external_refs)}")
    add(f"rebinds        : {len(result.rebinds)}")
    add("")

    add("WARNINGS BY KIND")
    add("-" * 72)
    if not result.warnings:
        add("  none")
    else:
        shapes: dict[str, int] = {}
        for warning in result.warnings:
            key = re.sub(r"\d+", "N", re.sub(r'"[^"]*"', '"X"', str(warning)))
            shapes[key] = shapes.get(key, 0) + 1
        for key, count in sorted(shapes.items(), key=lambda item: -item[1]):
            add(f"  {count:6}x  {key}")
    add("")

    add("NULLED REFERENCES BY TARGET CLASS")
    add("-" * 72)
    if not result.external_refs:
        add("  none")
    else:
        classes: dict[str, int] = {}
        for reference in result.external_refs:
            name = str(reference).split(" ", 1)[0]
            classes[name] = classes.get(name, 0) + 1
        for name, count in sorted(classes.items(), key=lambda item: -item[1]):
            add(f"  {count:6}x  {name}")
    add("")

    add("REBINDS BY TYPE AND TARGET")
    add("-" * 72)
    if not result.rebinds:
        add("  none")
    else:
        pairs: dict[tuple[str, str], int] = {}
        for entry in result.rebinds:
            key = (str(entry.get("type", "")).split(".")[-1], str(entry.get("name", "")))
            pairs[key] = pairs.get(key, 0) + 1
        for (kind, name), count in sorted(pairs.items(), key=lambda item: -item[1]):
            add(f"  {count:6}x  {kind:32} {name}")
    add("")

    add("FULL WARNING LIST")
    add("-" * 72)
    for warning in result.warnings or ["  none"]:
        add(f"  {warning}")
    add("")

    add("FULL NULLED REFERENCE LIST")
    add("-" * 72)
    for reference in result.external_refs or ["  none"]:
        add(f"  {reference}")
    add("")

    add("FULL REBIND LIST")
    add("-" * 72)
    for entry in result.rebinds or []:
        add(
            f"  {entry.get('asset','')} :: {entry.get('node','')} :: "
            f"{entry.get('script','')}.{entry.get('memberPath','')} -> "
            f"{entry.get('name','')} ({entry.get('type','')})"
        )
    if not result.rebinds:
        add("  none")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result.report_path = path
    return path

