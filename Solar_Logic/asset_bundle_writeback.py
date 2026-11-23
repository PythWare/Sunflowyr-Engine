from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Callable
from .catalog import CatalogIndex, find_catalog_record, load_catalog_indexes
from .catalog import StagedPayload
from .class_ids import class_name
from .taildata import read_taildata

class WritebackAssetError(ValueError):
    pass

def repack_audio_writeback(path: Path, record: dict[str, Any], data_root: Path):
    from .audio_repack import repack_audio_view

    replacement = repack_audio_view(path, record, data_root)
    return replacement.object_payload, replacement.resource_payload, "audio"

def repack_raw_object(path: Path, record: dict[str, Any], data_root: Path) -> bytes:
    data = Path(path).read_bytes()
    if not data and int(record.get("byteSize") or record.get("byte_size") or 0) != 0:
        raise WritebackAssetError(f"{Path(path).name}: raw object file is empty")
    return data

def repackers() -> dict[str, Callable[..., bytes]]:
    from .animation_package import repack_animation_clip_glb, repack_animation_package
    from .animation_repack import repack_animation_clip_json
    from .cgprogram_repack import repack_cgprogram_json
    from .editable_object_repack import repack_editable_asset_json
    from .font_repack import repack_font_data_file, repack_font_json
    from .mesh_repack import repack_mesh_glb
    from .texture_repack import repack_texture_png
    from .map_component_repack import repack_map_component_json
    from .material_repack import repack_material_json
    from .mono_repack import repack_monobehaviour_json, repack_monoscript_json
    from .navmesh_repack import repack_navmesh_data_json
    from .shader_package import repack_shader_package
    from .sprite_repack import repack_sprite_json
    from .terrain_glb_repack import repack_terrain_glb
    from .terrain_repack import repack_terrain_data_json
    from .text_asset_repack import repack_text_asset
    from .textmesh_repack import repack_textmesh_json
    from .ui_repack import repack_ui_component_json

    table: dict[str, Callable[..., bytes]] = {
        "animation_clip_json": repack_animation_clip_json,
        "cgprogram_json": repack_cgprogram_json,
        "font_json": repack_font_json,
        "material_json": repack_material_json,
        "monobehaviour_json": repack_monobehaviour_json,
        "monoscript_json": repack_monoscript_json,
        "navmesh_data_json": repack_navmesh_data_json,
        "shader_package": repack_shader_package,
        "sprite_json": repack_sprite_json,
        "terrain_data_json": repack_terrain_data_json,
        "reconstructed_terrain_glb": repack_terrain_glb,
        "texture_png": repack_texture_png,
        "mesh_glb": repack_mesh_glb,
        "animation_clip_package": repack_animation_package,
        "animation_clip_glb": repack_animation_clip_glb,
        "font_data": repack_font_data_file,
        "audio_ogg": repack_audio_writeback,
        "audio_wav": repack_audio_writeback,
        "raw_object": repack_raw_object,
        "text_asset": repack_text_asset,
        "textmesh_json": repack_textmesh_json,
    }
    for kind in (
        "canvas_json",
        "canvas_renderer_json",
        "rect_transform_json",
        "canvas_group_json",
    ):
        table[kind] = repack_ui_component_json
    for kind in MAP_COMPONENT_KINDS:
        table[kind] = repack_map_component_json
    from .editable_object_export import EDITABLE_ASSET_KINDS

    for kind in EDITABLE_ASSET_KINDS:
        table.setdefault(kind, repack_editable_asset_json)
    from .editable_object_export import EDITABLE_ASSET_SCHEMAS
    from .editable_table import TABLE_ASSET_CLASS_IDS, repack_editable_asset_table

    for class_id in TABLE_ASSET_CLASS_IDS:
        table[EDITABLE_ASSET_SCHEMAS[class_id][0]] = repack_editable_asset_table
    return table

MAP_COMPONENT_KINDS = {
    "transform_json", "camera_json", "mesh_renderer_json", "mesh_filter_json",
    "occlusion_culling_settings_json", "rigidbody_json", "mesh_collider_json",
    "box_collider_json", "audio_source_json", "animator_json", "trail_renderer_json",
    "render_settings_json", "light_json", "animation_json", "line_renderer_json",
    "lens_flare_json", "flare_layer_json", "physic_material_json", "sphere_collider_json",
    "capsule_collider_json", "skinned_mesh_renderer_json", "character_controller_json",
    "terrain_collider_json", "lightmap_settings_json", "audio_reverb_zone_json",
    "wind_zone_json", "navmesh_settings_json", "particle_system_json",
    "particle_system_renderer_json", "lod_group_json", "reflection_probe_json",
    "billboard_renderer_json", "light_probes_json",
}

def try_build_writeback_asset(
    path: Path,
    data_root: Path | None,
) -> tuple[bytes, str, int, dict[str, object], bytes | None, str | None,
           tuple[str, str, str] | None] | None:
    if data_root is None:
        return None
    path = Path(path)
    record = read_taildata(path)
    if not isinstance(record, dict):
        return None
    return build_writeback_asset(path, record, Path(data_root))

def build_writeback_asset(
    path: Path,
    record: dict[str, Any],
    data_root: Path,
) -> tuple[bytes, str, int, dict[str, object], bytes | None, str | None,
           tuple[str, str, str] | None] | None:
    path = Path(path)
    kind = str(record.get("kind") or "")
    repack = repackers().get(kind)
    if repack is None:
        return None
    if kind != "raw_object" and not source_in_game(record, Path(data_root)):
        if kind in NEW_ASSET_KINDS:
            return None
        container = str(record.get("container") or "")
        if kind == "material_json":
            hint = "Stage it in the AssetBundle Generator, which turns it into a new material with its textures and shader."
        else:
            hint = "Stage the prefab GLB it belongs to instead, the prefab rebuild carries its components."
        raise WritebackAssetError(f"{path.name} came from {container}, which isnt part of RWBY Data, so it cant replace a game asset. {hint}")
    class_id = int(record.get("classId") or record.get("class_id") or 0)
    if class_id <= 0:
        raise WritebackAssetError(f"{path.name}: taildata record has no classId")
    try:
        produced = repack(path, record, Path(data_root))
    except Exception as exc:
        raise WritebackAssetError(f"{path.name}: {kind} write back failed: {exc}") from exc

    resource_payload: bytes | None = None
    resource_kind: str | None = None
    if isinstance(produced, tuple):
        payload, resource_payload, resource_kind = produced
    else:
        payload = produced

    name = str(record.get("name") or path.stem)
    script_ref = writeback_script_ref(path, class_id)
    details: dict[str, object] = {
        "sourceFormat": "sunflowyr-unpack-writeback",
        "kind": kind,
        "classId": class_id,
        "className": class_name(class_id),
        "payloadBytes": len(payload),
        "sourceContainer": str(record.get("container") or ""),
        "sourcePathId": int(record.get("pathId") or 0),
        "pointersAreGameRelative": True,
    }
    if resource_payload is not None:
        details["resourceBytes"] = len(resource_payload)
    if script_ref is not None:
        details["script"] = "%s/%s" % (script_ref[0], script_ref[2])
    return bytes(payload), name, class_id, details, resource_payload, resource_kind, script_ref

NEW_ASSET_KINDS = {"texture_png", "mesh_glb", "audio_ogg", "audio_wav", "font_data", "text_asset", "animation_clip_package"}

def source_in_game(record: dict[str, Any], data_root: Path) -> bool:
    container = str(record.get("container") or record.get("appendTarget") or record.get("append_target") or "")
    if not container:
        return True
    return (data_root / container).is_file()

def writeback_script_ref(path: Path, class_id: int) -> tuple[str, str, str] | None:
    from .asset_bundle_writer import MONO_BEHAVIOUR_CLASS_ID

    if class_id != MONO_BEHAVIOUR_CLASS_ID:
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WritebackAssetError(f"{path.name}: couldnt be read: {exc}") from exc
    script = document.get("solarScript")
    if not isinstance(script, dict):
        raise WritebackAssetError(
            f"{path.name}: this MonoBehaviour has no solarScript block, re-export it with a current unpack"
        )
    class_name = str(script.get("class_name") or script.get("name") or "")
    if not class_name:
        raise WritebackAssetError(f"{path.name}: solarScript names no class")
    assembly = str(script.get("assembly_name") or "Assembly-CSharp.dll")
    if assembly.lower().endswith(".dll"):
        assembly = assembly[:-4]
    return assembly, str(script.get("namespace") or ""), class_name
