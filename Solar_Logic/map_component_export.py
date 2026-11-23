from __future__ import annotations
import json, base64, struct
from pathlib import Path
from typing import Any
from .class_ids import class_name
from .model_context import ModelContextIndex, PPtr
from .particle_system_export import parse_particle_system
from .scene_component_export import (
    parse_audio_source,
    parse_lightmap_settings,
    parse_occlusion_culling_settings,
    parse_schema_component,
)
from .serialized import SerializedAsset, SerializedObject
from .paths import ensure_parent

MAP_COMPONENT_KINDS = {
    4: "transform_json",
    20: "camera_json",
    23: "mesh_renderer_json",
    29: "occlusion_culling_settings_json",
    33: "mesh_filter_json",
    54: "rigidbody_json",
    64: "mesh_collider_json",
    65: "box_collider_json",
    82: "audio_source_json",
    95: "animator_json",
    96: "trail_renderer_json",
    104: "render_settings_json",
    108: "light_json",
    111: "animation_json",
    120: "line_renderer_json",
    123: "lens_flare_json",
    124: "flare_layer_json",
    134: "physic_material_json",
    135: "sphere_collider_json",
    136: "capsule_collider_json",
    137: "skinned_mesh_renderer_json",
    143: "character_controller_json",
    154: "terrain_collider_json",
    157: "lightmap_settings_json",
    167: "audio_reverb_zone_json",
    182: "wind_zone_json",
    196: "navmesh_settings_json",
    198: "particle_system_json",
    199: "particle_system_renderer_json",
    205: "lod_group_json",
    215: "reflection_probe_json",
    227: "billboard_renderer_json",
    258: "light_probes_json",
}

MAP_SCHEMA_COMPONENTS = {
    95: "animator_schema.json",
    96: "trail_renderer_schema.json",
    111: "animation_component_schema.json",
    120: "line_renderer_schema.json",
    123: "lens_flare_schema.json",
    124: "flare_layer_schema.json",
    154: "terrain_collider_schema.json",
    196: "navmesh_settings_schema.json",
    258: "light_probes_schema.json",
}

class MapComponentExportError(Exception):
    pass

def parse_map_component_fields(
    asset: SerializedAsset,
    obj: SerializedObject,
    context: ModelContextIndex,
) -> dict[str, Any]:
    with asset.path.open("rb") as handle:
        handle.seek(obj.byte_start)
        data = handle.read(obj.byte_size)
    if len(data) != obj.byte_size:
        raise MapComponentExportError("component object data is truncated")
    schema_name = MAP_SCHEMA_COMPONENTS.get(obj.class_id)
    if schema_name is not None:
        fields = parse_schema_component(data, asset, context, schema_name)
    else:
        parser = {
            4: parse_transform,
            20: parse_camera,
            23: parse_mesh_renderer,
            29: parse_occlusion_culling_settings,
            33: parse_mesh_filter,
            54: parse_rigidbody,
            64: parse_mesh_collider,
            65: parse_box_collider,
            82: parse_audio_source,
            104: parse_render_settings,
            108: parse_light,
            134: parse_physic_material,
            135: parse_sphere_collider,
            136: parse_capsule_collider,
            137: parse_skinned_mesh_renderer,
            143: parse_character_controller,
            157: parse_lightmap_settings,
            167: parse_audio_reverb_zone,
            182: parse_wind_zone,
            198: parse_particle_system,
            199: parse_particle_system_renderer,
            205: parse_lod_group,
            215: parse_reflection_probe,
            227: parse_billboard_renderer,
        }.get(obj.class_id)
        if parser is None:
            from .editable_object_export import EDITABLE_ASSET_SCHEMAS, EDITABLE_COMPONENT_CLASS_IDS

            if obj.class_id not in EDITABLE_COMPONENT_CLASS_IDS or obj.class_id not in EDITABLE_ASSET_SCHEMAS:
                raise MapComponentExportError(f"unsupported map component class {obj.class_id}")
            return parse_schema_component(data, asset, context, EDITABLE_ASSET_SCHEMAS[obj.class_id][1])
        fields = parser(data, asset, context)
    return fields

def export_map_component_json(
    asset: SerializedAsset,
    obj: SerializedObject,
    context: ModelContextIndex,
    out_path: Path,
    object_metadata: dict[str, Any],
) -> dict[str, Any]:
    fields = parse_map_component_fields(asset, obj, context)
    game_object = fields.get("m_GameObject")
    game_object_name = game_object.get("name", "") if isinstance(game_object, dict) else ""
    binary_sidecars: list[dict[str, Any]] = []
    if obj.class_id == 29:
        pvs_data = fields.get("m_PVSData")
        if isinstance(pvs_data, dict) and pvs_data.get("encoding") == "base64":
            data_text = pvs_data.get("data")
            if not isinstance(data_text, str):
                raise MapComponentExportError("SceneSettings m_PVSData is missing base64 data")
            try:
                pvs_bytes = base64.b64decode(data_text, validate=True)
            except Exception as exc:
                raise MapComponentExportError("SceneSettings m_PVSData is invalid base64") from exc
            sidecar_path = out_path.with_suffix(".pvs.bin")
            sidecar_path.parent.mkdir(parents=True, exist_ok=True)
            sidecar_path.write_bytes(pvs_bytes)
            fields["m_PVSData"] = {
                "encoding": "external-binary",
                "path": sidecar_path.name,
                "byteLength": len(pvs_bytes),
            }
            binary_sidecars.append(
                {
                    "field": "m_PVSData",
                    "path": sidecar_path.name,
                    "byteLength": len(pvs_bytes),
                }
            )
    document = {
        "format": "sunflowyr-map-component",
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
        "gameObjectName": game_object_name,
        "fields": fields,
    }
    ensure_parent(out_path)
    out_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    conversion = {
        "format": "map_component_json_unity54",
        "classId": obj.class_id,
        "className": class_name(obj.class_id),
        "gameObjectName": game_object_name,
        "fieldCount": len(fields),
        "sourceByteLength": obj.byte_size,
        "binarySidecars": binary_sidecars,
    }
    object_metadata["conversion"] = conversion
    object_metadata["writeback_note"] = (
        "Map component JSON exposes Unity 5.4 fields. the AssetBundle generator rebuilds complete components from those fields. "
        "patch only layouts recover untouched bytes from the source object named by taildata."
    )
    return conversion

def parse_transform(data: bytes, asset: SerializedAsset, context: ModelContextIndex) -> dict:
    if len(data) < 68:
        raise MapComponentExportError("Transform object is too small")
    child_count = int_at(data, 52, asset)
    expected_size = 56 + (child_count + 1) * pointer_size(asset)
    if child_count < 0 or child_count > 1_000_000 or len(data) != expected_size:
        raise MapComponentExportError(
            f"Transform expected {expected_size} bytes for {child_count} children, found {len(data)}"
        )
    children = [
        pointer_at(data, 56 + index * pointer_size(asset), asset, context)
        for index in range(child_count)
    ]
    father_offset = 56 + child_count * pointer_size(asset)
    return {
        "m_GameObject": pointer_at(data, 0, asset, context),
        "m_LocalRotation": vector_at(data, 12, 4, asset),
        "m_LocalPosition": vector_at(data, 28, 3, asset),
        "m_LocalScale": vector_at(data, 40, 3, asset),
        "m_Children": children,
        "m_Father": pointer_at(data, father_offset, asset, context),
    }

def parse_rigidbody(data: bytes, asset: SerializedAsset, context: ModelContextIndex) -> dict:
    require_size(data, 36, "Rigidbody")
    return {
        "m_GameObject": pointer_at(data, 0, asset, context),
        "m_Mass": float_at(data, 12, asset),
        "m_Drag": float_at(data, 16, asset),
        "m_AngularDrag": float_at(data, 20, asset),
        "m_UseGravity": bool(data[24]),
        "m_IsKinematic": bool(data[25]),
        "m_Interpolate": data[26],
        "m_Constraints": int_at(data, 28, asset),
        "m_CollisionDetection": int_at(data, 32, asset),
    }

def parse_mesh_filter(data: bytes, asset: SerializedAsset, context: ModelContextIndex) -> dict:
    require_size(data, 24, "MeshFilter")
    return {
        "m_GameObject": pointer_at(data, 0, asset, context),
        "m_Mesh": pointer_at(data, 12, asset, context),
    }

def parse_mesh_renderer(data: bytes, asset: SerializedAsset, context: ModelContextIndex) -> dict:
    fields, offset = parse_renderer_base(data, asset, context)
    remaining = len(data) - offset
    if remaining == pointer_size(asset):
        fields["m_AdditionalVertexStreams"] = pointer_at(data, offset, asset, context)
        offset += pointer_size(asset)
    if offset != len(data):
        raise MapComponentExportError(
            f"MeshRenderer has {len(data) - offset} unparsed bytes"
        )
    return fields

def parse_billboard_renderer(
    data: bytes,
    asset: SerializedAsset,
    context: ModelContextIndex,
) -> dict:
    fields, offset = parse_renderer_base(data, asset, context)
    if len(data) - offset != pointer_size(asset):
        raise MapComponentExportError(
            f"BillboardRenderer expected one BillboardAsset pointer, found {len(data) - offset} bytes"
        )
    fields["m_Billboard"] = pointer_at(data, offset, asset, context)
    return fields

def parse_skinned_mesh_renderer(
    data: bytes,
    asset: SerializedAsset,
    context: ModelContextIndex,
) -> dict:
    fields, offset = parse_renderer_base(data, asset, context)
    fields["m_Quality"] = int_at(data, offset, asset)
    fields["m_UpdateWhenOffscreen"] = bool(data[offset + 4])
    fields["m_SkinnedMotionVectors"] = bool(data[offset + 5])
    offset += 8
    fields["m_Mesh"] = pointer_at(data, offset, asset, context)
    offset += pointer_size(asset)
    bone_count = int_at(data, offset, asset)
    offset += 4
    if bone_count < 0 or bone_count > 1_000_000:
        raise MapComponentExportError(f"invalid SkinnedMeshRenderer bone count {bone_count}")
    fields["m_Bones"] = [
        pointer_at(data, offset + index * pointer_size(asset), asset, context)
        for index in range(bone_count)
    ]
    offset += bone_count * pointer_size(asset)
    weight_count = int_at(data, offset, asset)
    offset += 4
    if weight_count < 0 or weight_count > 100_000_000:
        raise MapComponentExportError(
            f"invalid SkinnedMeshRenderer blend shape weight count {weight_count}"
        )
    fields["m_BlendShapeWeights"] = [
        float_at(data, offset + index * 4, asset) for index in range(weight_count)
    ]
    offset += weight_count * 4
    fields["m_RootBone"] = pointer_at(data, offset, asset, context)
    offset += pointer_size(asset)
    fields["m_AABB"] = {
        "m_Center": vector_at(data, offset, 3, asset),
        "m_Extent": vector_at(data, offset + 12, 3, asset),
    }
    offset += 24
    fields["m_DirtyAABB"] = bool(data[offset])
    offset = align_offset(offset + 1)
    if offset != len(data):
        raise MapComponentExportError(
            f"SkinnedMeshRenderer has {len(data) - offset} unparsed bytes"
        )
    return fields

def parse_particle_system_renderer(
    data: bytes,
    asset: SerializedAsset,
    context: ModelContextIndex,
) -> dict:
    fields, offset = parse_renderer_base(data, asset, context)
    fields.update(
        {
            "m_RenderMode": ushort_at(data, offset, asset),
            "m_SortMode": ushort_at(data, offset + 2, asset),
            "m_MinParticleSize": float_at(data, offset + 4, asset),
            "m_MaxParticleSize": float_at(data, offset + 8, asset),
            "m_CameraVelocityScale": float_at(data, offset + 12, asset),
            "m_VelocityScale": float_at(data, offset + 16, asset),
            "m_LengthScale": float_at(data, offset + 20, asset),
            "m_SortingFudge": float_at(data, offset + 24, asset),
            "m_NormalDirection": float_at(data, offset + 28, asset),
            "m_RenderAlignment": int_at(data, offset + 32, asset),
            "m_Pivot": vector_at(data, offset + 36, 3, asset),
        }
    )
    offset += 48
    for name in ("m_Mesh", "m_Mesh1", "m_Mesh2", "m_Mesh3"):
        fields[name] = pointer_at(data, offset, asset, context)
        offset += pointer_size(asset)
    if offset != len(data):
        raise MapComponentExportError(
            f"ParticleSystemRenderer has {len(data) - offset} unparsed bytes"
        )
    return fields

def parse_renderer_base(
    data: bytes,
    asset: SerializedAsset,
    context: ModelContextIndex,
) -> tuple[dict, int]:
    require_minimum_size(data, 120, "Renderer")
    fields = {
        "m_GameObject": pointer_at(data, 0, asset, context),
        "m_Enabled": bool(data[12]),
        "m_CastShadows": data[13],
        "m_ReceiveShadows": bool(data[14]),
        "m_MotionVectors": data[15],
        "m_LightProbeUsage": data[16],
        "m_ReflectionProbeUsage": data[17],
        "m_LightmapIndex": ushort_at(data, 20, asset),
        "m_LightmapIndexDynamic": ushort_at(data, 22, asset),
        "m_LightmapTilingOffset": vector_at(data, 24, 4, asset),
        "m_LightmapTilingOffsetDynamic": vector_at(data, 40, 4, asset),
    }
    material_count = int_at(data, 56, asset)
    if material_count < 0 or material_count > 1_000_000:
        raise MapComponentExportError(f"invalid renderer material count {material_count}")
    offset = 60
    fields["m_Materials"] = [
        pointer_at(data, offset + index * pointer_size(asset), asset, context)
        for index in range(material_count)
    ]
    offset += material_count * pointer_size(asset)
    subset_count = int_at(data, offset, asset)
    offset += 4
    if subset_count < 0 or subset_count > 100_000_000:
        raise MapComponentExportError(f"invalid renderer subset count {subset_count}")
    fields["m_SubsetIndices"] = [
        uint_at(data, offset + index * 4, asset) for index in range(subset_count)
    ]
    offset += subset_count * 4
    for name in ("m_StaticBatchRoot", "m_ProbeAnchor", "m_LightProbeVolumeOverride"):
        fields[name] = pointer_at(data, offset, asset, context)
        offset += pointer_size(asset)
    fields["m_SortingLayerID"] = uint_at(data, offset, asset)
    fields["m_SortingOrder"] = short_at(data, offset + 4, asset)
    offset = align_offset(offset + 6)
    return fields, offset

def parse_lod_group(data: bytes, asset: SerializedAsset, context: ModelContextIndex) -> dict:
    require_minimum_size(data, 44, "LODGroup")
    fields = {
        "m_GameObject": pointer_at(data, 0, asset, context),
        "m_LocalReferencePoint": vector_at(data, 12, 3, asset),
        "m_Size": float_at(data, 24, asset),
        "m_FadeMode": int_at(data, 28, asset),
        "m_AnimateCrossFading": bool(data[32]),
    }
    lod_count = int_at(data, 36, asset)
    if lod_count < 0 or lod_count > 100_000:
        raise MapComponentExportError(f"invalid LODGroup level count {lod_count}")
    offset = 40
    levels = []
    for level_index in range(lod_count):
        screen_height = float_at(data, offset, asset)
        fade_width = float_at(data, offset + 4, asset)
        renderer_count = int_at(data, offset + 8, asset)
        offset += 12
        if renderer_count < 0 or renderer_count > 1_000_000:
            raise MapComponentExportError(
                f"invalid LODGroup renderer count {renderer_count} at level {level_index}"
            )
        renderers = [
            {
                "renderer": pointer_at(
                    data,
                    offset + index * pointer_size(asset),
                    asset,
                    context,
                )
            }
            for index in range(renderer_count)
        ]
        offset += renderer_count * pointer_size(asset)
        levels.append(
            {
                "screenRelativeHeight": screen_height,
                "fadeTransitionWidth": fade_width,
                "renderers": renderers,
            }
        )
    fields["m_LODs"] = levels
    fields["m_Enabled"] = bool(data[offset])
    offset = align_offset(offset + 1)
    if offset != len(data):
        raise MapComponentExportError(f"LODGroup has {len(data) - offset} unparsed bytes")
    return fields

def parse_reflection_probe(
    data: bytes,
    asset: SerializedAsset,
    context: ModelContextIndex,
) -> dict:
    require_size(data, 140, "ReflectionProbe")
    return {
        "m_GameObject": pointer_at(data, 0, asset, context),
        "m_Enabled": bool(data[12]),
        "m_Type": data[13],
        "m_Mode": data[14],
        "m_RefreshMode": data[15],
        "m_TimeSlicingMode": data[16],
        "m_RuntimeTexture": pointer_at(data, 20, asset, context),
        "m_Resolution": int_at(data, 32, asset),
        "m_UpdateFrequency": int_at(data, 36, asset),
        "m_BoxSize": vector_at(data, 40, 3, asset),
        "m_BoxOffset": vector_at(data, 52, 3, asset),
        "m_NearClip": float_at(data, 64, asset),
        "m_FarClip": float_at(data, 68, asset),
        "m_ShadowDistance": float_at(data, 72, asset),
        "m_ClearFlags": int_at(data, 76, asset),
        "m_BackGroundColor": color_at(data, 80, asset),
        "m_CullingMask": uint_at(data, 96, asset),
        "m_IntensityMultiplier": float_at(data, 100, asset),
        "m_BlendDistance": float_at(data, 104, asset),
        "m_HDR": bool(data[108]),
        "m_BoxProjection": bool(data[109]),
        "m_RenderDynamicObjects": bool(data[110]),
        "m_UseOcclusionCulling": bool(data[111]),
        "m_Importance": int_at(data, 112, asset),
        "m_CustomBakedTexture": pointer_at(data, 116, asset, context),
        "m_BakedTexture": pointer_at(data, 128, asset, context),
    }

def parse_character_controller(
    data: bytes,
    asset: SerializedAsset,
    context: ModelContextIndex,
) -> dict:
    require_size(data, 64, "CharacterController")
    return {
        "m_GameObject": pointer_at(data, 0, asset, context),
        "m_Material": pointer_at(data, 12, asset, context),
        "m_IsTrigger": bool(data[24]),
        "m_Enabled": bool(data[25]),
        "m_Height": float_at(data, 28, asset),
        "m_Radius": float_at(data, 32, asset),
        "m_SlopeLimit": float_at(data, 36, asset),
        "m_StepOffset": float_at(data, 40, asset),
        "m_SkinWidth": float_at(data, 44, asset),
        "m_MinMoveDistance": float_at(data, 48, asset),
        "m_Center": vector_at(data, 52, 3, asset),
    }

def parse_audio_reverb_zone(
    data: bytes,
    asset: SerializedAsset,
    context: ModelContextIndex,
) -> dict:
    require_size(data, 84, "AudioReverbZone")
    return {
        "m_GameObject": pointer_at(data, 0, asset, context),
        "m_Enabled": bool(data[12]),
        "m_MinDistance": float_at(data, 16, asset),
        "m_MaxDistance": float_at(data, 20, asset),
        "m_ReverbPreset": int_at(data, 24, asset),
        "m_Room": int_at(data, 28, asset),
        "m_RoomHF": int_at(data, 32, asset),
        "m_DecayTime": float_at(data, 36, asset),
        "m_DecayHFRatio": float_at(data, 40, asset),
        "m_Reflections": int_at(data, 44, asset),
        "m_ReflectionsDelay": float_at(data, 48, asset),
        "m_Reverb": int_at(data, 52, asset),
        "m_ReverbDelay": float_at(data, 56, asset),
        "m_HFReference": float_at(data, 60, asset),
        "m_RoomRolloffFactor": float_at(data, 64, asset),
        "m_Diffusion": float_at(data, 68, asset),
        "m_Density": float_at(data, 72, asset),
        "m_LFReference": float_at(data, 76, asset),
        "m_RoomLF": int_at(data, 80, asset),
    }

def parse_wind_zone(data: bytes, asset: SerializedAsset, context: ModelContextIndex) -> dict:
    require_size(data, 40, "WindZone")
    return {
        "m_GameObject": pointer_at(data, 0, asset, context),
        "m_Enabled": bool(data[12]),
        "m_Mode": int_at(data, 16, asset),
        "m_Radius": float_at(data, 20, asset),
        "m_WindMain": float_at(data, 24, asset),
        "m_WindTurbulence": float_at(data, 28, asset),
        "m_WindPulseMagnitude": float_at(data, 32, asset),
        "m_WindPulseFrequency": float_at(data, 36, asset),
    }

def parse_physic_material(
    data: bytes,
    asset: SerializedAsset,
    context: ModelContextIndex,
) -> dict:
    if len(data) < 24:
        raise MapComponentExportError("PhysicMaterial object is too small")
    name_length = int_at(data, 0, asset)
    if name_length < 0 or name_length > len(data) - 4:
        raise MapComponentExportError(f"invalid PhysicMaterial name length {name_length}")
    name_end = 4 + name_length
    offset = align_offset(name_end)
    if offset + 20 != len(data):
        raise MapComponentExportError(
            f"PhysicMaterial expected 20 property bytes after its name, found {len(data) - offset}"
        )
    return {
        "m_Name": data[4:name_end].decode("utf-8", "replace"),
        "dynamicFriction": float_at(data, offset, asset),
        "staticFriction": float_at(data, offset + 4, asset),
        "bounciness": float_at(data, offset + 8, asset),
        "frictionCombine": int_at(data, offset + 12, asset),
        "bounceCombine": int_at(data, offset + 16, asset),
    }

def parse_render_settings(
    data: bytes,
    asset: SerializedAsset,
    context: ModelContextIndex,
) -> dict:
    require_size(data, 316, "RenderSettings")
    return {
        "m_Fog": bool(data[0]),
        "m_FogColor": color_at(data, 4, asset),
        "m_FogMode": int_at(data, 20, asset),
        "m_FogDensity": float_at(data, 24, asset),
        "m_LinearFogStart": float_at(data, 28, asset),
        "m_LinearFogEnd": float_at(data, 32, asset),
        "m_AmbientSkyColor": color_at(data, 36, asset),
        "m_AmbientEquatorColor": color_at(data, 52, asset),
        "m_AmbientGroundColor": color_at(data, 68, asset),
        "m_AmbientIntensity": float_at(data, 84, asset),
        "m_AmbientMode": int_at(data, 88, asset),
        "m_SkyboxMaterial": pointer_at(data, 92, asset, context),
        "m_HaloStrength": float_at(data, 104, asset),
        "m_FlareStrength": float_at(data, 108, asset),
        "m_FlareFadeSpeed": float_at(data, 112, asset),
        "m_HaloTexture": pointer_at(data, 116, asset, context),
        "m_SpotCookie": pointer_at(data, 128, asset, context),
        "m_DefaultReflectionMode": int_at(data, 140, asset),
        "m_DefaultReflectionResolution": int_at(data, 144, asset),
        "m_ReflectionBounces": int_at(data, 148, asset),
        "m_ReflectionIntensity": float_at(data, 152, asset),
        "m_CustomReflection": pointer_at(data, 156, asset, context),
        "m_AmbientProbe": {"sh": [exact_float_at(data, 168 + index * 4, asset) for index in range(27)]},
        "m_GeneratedSkyboxReflection": pointer_at(data, 276, asset, context),
        "m_Sun": pointer_at(data, 288, asset, context),
        "m_IndirectSpecularColor": color_at(data, 300, asset),
    }

def parse_mesh_collider(data: bytes, asset: SerializedAsset, context: ModelContextIndex) -> dict:
    require_size(data, 44, "MeshCollider")
    return {
        "m_GameObject": pointer_at(data, 0, asset, context),
        "m_Material": pointer_at(data, 12, asset, context),
        "m_IsTrigger": bool(data[24]),
        "m_Enabled": bool(data[25]),
        "m_Convex": bool(data[28]),
        "m_Mesh": pointer_at(data, 32, asset, context),
    }

def parse_box_collider(data: bytes, asset: SerializedAsset, context: ModelContextIndex) -> dict:
    require_size(data, 52, "BoxCollider")
    return {
        "m_GameObject": pointer_at(data, 0, asset, context),
        "m_Material": pointer_at(data, 12, asset, context),
        "m_IsTrigger": bool(data[24]),
        "m_Enabled": bool(data[25]),
        "m_Size": vector_at(data, 28, 3, asset),
        "m_Center": vector_at(data, 40, 3, asset),
    }

def parse_sphere_collider(data: bytes, asset: SerializedAsset, context: ModelContextIndex) -> dict:
    require_size(data, 44, "SphereCollider")
    return {
        "m_GameObject": pointer_at(data, 0, asset, context),
        "m_Material": pointer_at(data, 12, asset, context),
        "m_IsTrigger": bool(data[24]),
        "m_Enabled": bool(data[25]),
        "m_Radius": float_at(data, 28, asset),
        "m_Center": vector_at(data, 32, 3, asset),
    }

def parse_capsule_collider(data: bytes, asset: SerializedAsset, context: ModelContextIndex) -> dict:
    require_size(data, 52, "CapsuleCollider")
    return {
        "m_GameObject": pointer_at(data, 0, asset, context),
        "m_Material": pointer_at(data, 12, asset, context),
        "m_IsTrigger": bool(data[24]),
        "m_Enabled": bool(data[25]),
        "m_Radius": float_at(data, 28, asset),
        "m_Height": float_at(data, 32, asset),
        "m_Direction": int_at(data, 36, asset),
        "m_Center": vector_at(data, 40, 3, asset),
    }

def parse_camera(data: bytes, asset: SerializedAsset, context: ModelContextIndex) -> dict:
    require_size(data, 117, "Camera")
    return {
        "m_GameObject": pointer_at(data, 0, asset, context),
        "m_Enabled": bool(data[12]),
        "m_ClearFlags": int_at(data, 16, asset),
        "m_BackGroundColor": color_at(data, 20, asset),
        "m_NormalizedViewPortRect": rect_at(data, 36, asset),
        "near clip plane": float_at(data, 52, asset),
        "far clip plane": float_at(data, 56, asset),
        "field of view": float_at(data, 60, asset),
        "orthographic": bool(data[64]),
        "orthographic size": float_at(data, 68, asset),
        "m_Depth": float_at(data, 72, asset),
        "m_CullingMask": uint_at(data, 76, asset),
        "m_RenderingPath": int_at(data, 80, asset),
        "m_TargetTexture": pointer_at(data, 84, asset, context),
        "m_TargetDisplay": int_at(data, 96, asset),
        "m_TargetEye": int_at(data, 100, asset),
        "m_HDR": bool(data[104]),
        "m_OcclusionCulling": bool(data[105]),
        "m_StereoConvergence": float_at(data, 108, asset),
        "m_StereoSeparation": float_at(data, 112, asset),
        "m_StereoMirrorMode": bool(data[116]),
    }

def parse_light(data: bytes, asset: SerializedAsset, context: ModelContextIndex) -> dict:
    require_size(data, 136, "Light")
    return {
        "m_GameObject": pointer_at(data, 0, asset, context),
        "m_Enabled": bool(data[12]),
        "m_Type": int_at(data, 16, asset),
        "m_Color": color_at(data, 20, asset),
        "m_Intensity": float_at(data, 36, asset),
        "m_Range": float_at(data, 40, asset),
        "m_SpotAngle": float_at(data, 44, asset),
        "m_CookieSize": float_at(data, 48, asset),
        "m_Shadows": {
            "m_Type": int_at(data, 52, asset),
            "m_Resolution": int_at(data, 56, asset),
            "m_CustomResolution": int_at(data, 60, asset),
            "m_Strength": float_at(data, 64, asset),
            "m_Bias": float_at(data, 68, asset),
            "m_NormalBias": float_at(data, 72, asset),
            "m_NearPlane": float_at(data, 76, asset),
        },
        "m_Cookie": pointer_at(data, 80, asset, context),
        "m_DrawHalo": bool(data[92]),
        "m_BakedIndex": int_at(data, 96, asset),
        "m_Flare": pointer_at(data, 100, asset, context),
        "m_RenderMode": int_at(data, 112, asset),
        "m_CullingMask": uint_at(data, 116, asset),
        "m_Lightmapping": int_at(data, 120, asset),
        "m_AreaSize": vector_at(data, 124, 2, asset),
        "m_BounceIntensity": float_at(data, 132, asset),
    }

def pointer_at(
    data: bytes,
    offset: int,
    asset: SerializedAsset,
    context: ModelContextIndex,
) -> dict:
    file_id = int_at(data, offset, asset)
    if asset.format_version >= 14:
        path_id = struct.unpack_from(asset.endian + "q", data, offset + 4)[0]
    else:
        path_id = int_at(data, offset + 4, asset)
    pointer = PPtr(file_id, path_id)
    asset_key = asset.relative_path.replace("\\", "/").lower()
    resolved_key = context.resolve(asset_key, pointer)
    target = context.objects.get(resolved_key) if resolved_key is not None else None
    document = {
        "fileId": file_id,
        "pathId": path_id,
        "resolved": target is not None,
    }
    if target is None or resolved_key is None:
        return document
    name = ""
    if resolved_key in context.game_objects:
        name = context.game_objects[resolved_key].name
    elif resolved_key in context.materials:
        name = context.materials[resolved_key].name
    elif resolved_key in context.texture_names:
        name = context.texture_names[resolved_key]
    document.update(
        {
            "container": resolved_key[0],
            "resolvedPathId": resolved_key[1],
            "classId": target.class_id,
            "className": class_name(target.class_id),
            "name": name,
        }
    )
    return document

def vector_at(data: bytes, offset: int, length: int, asset: SerializedAsset) -> dict:
    values = struct.unpack_from(asset.endian + f"{length}f", data, offset)
    return dict(zip(("x", "y", "z", "w"), values))

def color_at(data: bytes, offset: int, asset: SerializedAsset) -> dict:
    values = struct.unpack_from(asset.endian + "4f", data, offset)
    return dict(zip(("r", "g", "b", "a"), values))

def rect_at(data: bytes, offset: int, asset: SerializedAsset) -> dict:
    values = struct.unpack_from(asset.endian + "4f", data, offset)
    return dict(zip(("x", "y", "width", "height"), values))

def int_at(data: bytes, offset: int, asset: SerializedAsset) -> int:
    return struct.unpack_from(asset.endian + "i", data, offset)[0]

def uint_at(data: bytes, offset: int, asset: SerializedAsset) -> int:
    return struct.unpack_from(asset.endian + "I", data, offset)[0]

def short_at(data: bytes, offset: int, asset: SerializedAsset) -> int:
    return struct.unpack_from(asset.endian + "h", data, offset)[0]

def ushort_at(data: bytes, offset: int, asset: SerializedAsset) -> int:
    return struct.unpack_from(asset.endian + "H", data, offset)[0]

def float_at(data: bytes, offset: int, asset: SerializedAsset) -> float:
    return struct.unpack_from(asset.endian + "f", data, offset)[0]

def exact_float_at(data: bytes, offset: int, asset: SerializedAsset) -> float | dict:
    value = float_at(data, offset, asset)
    if value != value:
        return {"f32Bits": f"{struct.unpack_from(asset.endian + 'I', data, offset)[0]:08x}"}
    return value

def require_size(data: bytes, expected: int, label: str) -> None:
    if len(data) != expected:
        raise MapComponentExportError(f"{label} expected {expected} bytes, found {len(data)}")

def require_minimum_size(data: bytes, expected: int, label: str) -> None:
    if len(data) < expected:
        raise MapComponentExportError(f"{label} expected at least {expected} bytes, found {len(data)}")

def align_offset(offset: int, alignment: int = 4) -> int:
    return offset + (-offset % alignment)

def pointer_size(asset: SerializedAsset) -> int:
    return 12 if asset.format_version >= 14 else 8
