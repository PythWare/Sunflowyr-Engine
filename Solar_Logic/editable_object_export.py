from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from .class_ids import class_name
from .model_context import ModelContextIndex
from .particle_system_export import decorate_pointers
from .serialized import SerializedAsset, SerializedObject
from .unity_typetree_codec import load_schema, read_typetree
from .paths import ensure_parent

EDITABLE_ASSET_SCHEMAS = {
    1: ("game_object_json", "game_object_schema.json"),
    5: ("time_manager_json", "time_manager_schema.json"),
    11: ("audio_manager_json", "audio_manager_schema.json"),
    13: ("input_manager_json", "input_manager_schema.json"),
    19: ("physics2d_settings_json", "physics2d_settings_schema.json"),
    30: ("graphics_settings_json", "graphics_settings_schema.json"),
    45: ("skybox_json", "skybox_schema.json"),
    47: ("quality_settings_json", "quality_settings_schema.json"),
    55: ("physics_manager_json", "physics_manager_schema.json"),
    78: ("tag_manager_json", "tag_manager_schema.json"),
    81: ("audio_listener_json", "audio_listener_schema.json"),
    91: ("animator_controller_json", "animator_controller_schema.json"),
    92: ("gui_layer_json", "gui_layer_schema.json"),
    94: ("script_mapper_json", "script_mapper_schema.json"),
    98: ("delayed_call_manager_json", "delayed_call_manager_schema.json"),
    116: ("mono_manager_json", "mono_manager_schema.json"),
    119: ("projector_json", "projector_schema.json"),
    121: ("flare_json", "flare_schema.json"),
    126: ("navmesh_areas_json", "navmesh_areas_schema.json"),
    129: ("player_settings_json", "player_settings_schema.json"),
    141: ("build_settings_json", "build_settings_schema.json"),
    147: ("resource_manager_json", "resource_manager_schema.json"),
    148: ("network_view_json", "network_view_schema.json"),
    149: ("network_manager_json", "network_manager_schema.json"),
    150: ("preload_data_json", "preload_data_schema.json"),
    155: ("master_server_interface_json", "master_server_interface_schema.json"),
    193: ("tree_json", "tree_schema.json"),
    195: ("navmesh_agent_json", "navmesh_agent_schema.json"),
    218: ("terrain_json", "terrain_component_schema.json"),
    226: ("billboard_asset_json", "billboard_asset_schema.json"),
    228: ("speedtree_wind_asset_json", "speedtree_wind_asset_schema.json"),
    241: ("audio_mixer_controller_json", "audio_mixer_controller_schema.json"),
    243: ("audio_mixer_group_json", "audio_mixer_group_schema.json"),
    245: ("audio_mixer_snapshot_json", "audio_mixer_snapshot_schema.json"),
    300: ("runtime_initialize_manager_json", "runtime_initialize_manager_schema.json"),
}

EDITABLE_COMPONENT_CLASS_IDS = {
    45,
    81,
    92,
    119,
    148,
    193,
    195,
    218,
}

EDITABLE_ASSET_KINDS = {
    definition[0] for definition in EDITABLE_ASSET_SCHEMAS.values()
}

class EditableObjectExportError(ValueError):
    pass

def export_editable_asset_json(
    asset: SerializedAsset,
    obj: SerializedObject,
    context: ModelContextIndex,
    out_path: Path,
    metadata: dict[str, Any],
    export_name: str,
) -> dict[str, Any]:
    definition = EDITABLE_ASSET_SCHEMAS.get(obj.class_id)
    if definition is None:
        raise EditableObjectExportError(f"unsupported editable asset class {obj.class_id}")
    kind, schema_name = definition
    with asset.path.open("rb") as handle:
        handle.seek(obj.byte_start)
        data = handle.read(obj.byte_size)
    if len(data) != obj.byte_size:
        raise EditableObjectExportError("editable asset object data is truncated")

    fields = read_typetree(data, load_schema(schema_name), asset.endian)
    decorate_pointers(fields, asset, context)
    name = fields.get("m_Name")
    if not isinstance(name, str) or not name:
        name = export_name
    game_object = fields.get("m_GameObject")
    game_object_name = ""
    if isinstance(game_object, dict):
        game_object_name = str(game_object.get("name") or "")
    document = {
        "format": "sunflowyr-editable-asset",
        "version": 1,
        "game": "rwby-ge",
        "unityVersion": asset.unity_version,
        "source": {
            "container": asset.relative_path,
            "pathId": obj.path_id,
            "classId": obj.class_id,
            "className": class_name(obj.class_id),
            "byteOffset": obj.byte_start,
            "byteSize": obj.byte_size,
        },
        "name": name,
        "gameObjectName": game_object_name,
        "fields": fields,
    }
    ensure_parent(out_path)
    out_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    conversion = {
        "format": "editable_asset_json_unity54",
        "kind": kind,
        "classId": obj.class_id,
        "className": class_name(obj.class_id),
        "name": name,
        "gameObjectName": game_object_name,
        "fieldCount": len(fields),
        "sourceByteLength": len(data),
    }
    metadata["conversion"] = conversion
    metadata["writeback_note"] = (
        f"{class_name(obj.class_id)} JSON exposes the complete Unity 5.4 type-tree fields. "
        "The writer rebuilds directly from those fields."
    )
    return conversion
