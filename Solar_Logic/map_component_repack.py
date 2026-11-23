from __future__ import annotations
import base64, json, struct
from pathlib import Path
from typing import Any
from .mono_repack import EndianWriter, taildata_value
from .map_component_export import MAP_SCHEMA_COMPONENTS
from .particle_system_export import build_particle_system
from .scene_component_export import (
    build_audio_source,
    build_lightmap_settings,
    build_occlusion_culling_settings,
    build_schema_component,
)
from .serialized import SerializedAsset, load_serialized_asset

class MapComponentRepackError(ValueError):
    pass

asset_cache: dict[tuple[str, str], SerializedAsset] = {}

def repack_map_component_json(
    json_path: Path,
    taildata: dict[str, Any],
    data_root: Path,
) -> bytes:
    document = json.loads(Path(json_path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise MapComponentRepackError("map component JSON root must be an object")
    container = str(taildata_value(taildata, "container") or "")
    if not container:
        raise MapComponentRepackError("taildata record doesnt name a Unity container")
    asset = load_component_asset(Path(data_root), container)
    class_id = int(taildata_value(taildata, "classId", 0) or 0)
    fields = document.get("fields")
    if not isinstance(fields, dict):
        raise MapComponentRepackError("map component JSON is missing fields")
    hydrate_map_binary_fields(fields, Path(json_path))
    schema_name = MAP_SCHEMA_COMPONENTS.get(class_id)
    if schema_name is not None:
        return build_schema_component(fields, asset, schema_name)
    builder = {
        4: build_transform,
        23: build_mesh_renderer,
        29: build_occlusion_culling_settings,
        33: build_mesh_filter,
        82: build_audio_source,
        104: build_render_settings,
        134: build_physic_material,
        137: build_skinned_mesh_renderer,
        157: build_lightmap_settings,
        198: build_particle_system,
        199: build_particle_system_renderer,
        205: build_lod_group,
        215: build_reflection_probe,
        227: build_billboard_renderer,
    }.get(class_id)
    if builder is not None:
        return builder(fields, asset)
    writer = {
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
    }.get(class_id)
    if writer is None:
        raise MapComponentRepackError(f"unsupported map component class {class_id}")
    data = load_patch_source_bytes(document, taildata, asset, class_id)
    writer(data, fields, asset)
    return bytes(data)

def load_patch_source_bytes(
    document: dict[str, Any],
    taildata: dict[str, Any],
    asset: SerializedAsset,
    class_id: int,
) -> bytearray:
    preserved = document.get("preservedRaw")
    if isinstance(preserved, dict) and preserved.get("encoding") == "base64":
        raw_text = preserved.get("data")
        if not isinstance(raw_text, str):
            raise MapComponentRepackError("preservedRaw.data must be base64 text")
        try:
            data = bytearray(base64.b64decode(raw_text, validate=True))
        except Exception as exc:
            raise MapComponentRepackError("preservedRaw.data is invalid base64") from exc
        expected_size = int(preserved.get("byteLength", len(data)))
        if len(data) != expected_size:
            raise MapComponentRepackError("preservedRaw byte length doesnt match the JSON declaration")
        return data

    path_id = int(taildata_value(taildata, "pathId", 0) or 0)
    source_object = next((item for item in asset.objects if item.path_id == path_id), None)
    if source_object is None:
        raise MapComponentRepackError(f"source component path ID {path_id} was not found")
    if source_object.class_id != class_id:
        raise MapComponentRepackError(
            f"source component path ID {path_id} has class {source_object.class_id}, expected {class_id}"
        )
    with asset.path.open("rb") as handle:
        handle.seek(source_object.byte_start)
        data = bytearray(handle.read(source_object.byte_size))
    if len(data) != source_object.byte_size:
        raise MapComponentRepackError("source component object data is truncated")
    return data

def hydrate_map_binary_fields(fields: dict[str, Any], json_path: Path) -> None:
    pvs_data = fields.get("m_PVSData")
    if not isinstance(pvs_data, dict) or pvs_data.get("encoding") != "external-binary":
        return
    relative_path = Path(str(pvs_data.get("path") or ""))
    base = Path(json_path).resolve().parent
    binary_path = (base / relative_path).resolve()
    try:
        binary_path.relative_to(base)
    except ValueError as exc:
        raise MapComponentRepackError("m_PVSData sidecar path escapes the JSON folder") from exc
    if not binary_path.is_file():
        raise MapComponentRepackError(f"m_PVSData sidecar wasnt found: {binary_path}")
    data = binary_path.read_bytes()
    expected_size = int(pvs_data.get("byteLength", len(data)))
    if len(data) != expected_size:
        raise MapComponentRepackError(
            f"m_PVSData sidecar expected {expected_size} bytes, found {len(data)}"
        )
    fields["m_PVSData"] = {
        "encoding": "base64",
        "byteLength": len(data),
        "data": base64.b64encode(data).decode("ascii"),
    }

def load_component_asset(data_root: Path, container: str) -> SerializedAsset:
    root = data_root.resolve()
    key = (str(root).lower(), container.replace("\\", "/").lower())
    asset = asset_cache.get(key)
    if asset is None:
        asset = load_serialized_asset((root / container).resolve(), root)
        asset_cache[key] = asset
    return asset

def build_transform(fields: dict, asset: SerializedAsset) -> bytes:
    writer = EndianWriter(asset.endian)
    write_pointer(writer, asset, fields.get("m_GameObject"))
    write_vector(writer, fields.get("m_LocalRotation"), 4, (0.0, 0.0, 0.0, 1.0))
    write_vector(writer, fields.get("m_LocalPosition"), 3, (0.0, 0.0, 0.0))
    write_vector(writer, fields.get("m_LocalScale"), 3, (1.0, 1.0, 1.0))
    children = fields.get("m_Children")
    if not isinstance(children, list):
        children = []
    writer.i32(len(children))
    for child in children:
        write_pointer(writer, asset, child)
    write_pointer(writer, asset, fields.get("m_Father"))
    return writer.bytes()

def build_mesh_renderer(fields: dict, asset: SerializedAsset) -> bytes:
    writer = EndianWriter(asset.endian)
    write_renderer_base(writer, fields, asset)
    if "m_AdditionalVertexStreams" in fields:
        write_pointer(writer, asset, fields.get("m_AdditionalVertexStreams"))
    elif "m_LightmapParameters" in fields:
        write_pointer(writer, asset, fields.get("m_LightmapParameters"))
    return writer.bytes()

def build_mesh_filter(fields: dict, asset: SerializedAsset) -> bytes:
    writer = EndianWriter(asset.endian)
    write_pointer(writer, asset, fields.get("m_GameObject"))
    write_pointer(writer, asset, fields.get("m_Mesh"))
    return writer.bytes()

def build_billboard_renderer(fields: dict, asset: SerializedAsset) -> bytes:
    writer = EndianWriter(asset.endian)
    write_renderer_base(writer, fields, asset)
    write_pointer(writer, asset, fields.get("m_Billboard"))
    return writer.bytes()

def build_skinned_mesh_renderer(fields: dict, asset: SerializedAsset) -> bytes:
    writer = EndianWriter(asset.endian)
    write_renderer_base(writer, fields, asset)
    writer.i32(int(fields.get("m_Quality", 0)))
    writer.bool(fields.get("m_UpdateWhenOffscreen", False))
    writer.bool(fields.get("m_SkinnedMotionVectors", False))
    writer.align()
    write_pointer(writer, asset, fields.get("m_Mesh"))
    bones = fields.get("m_Bones")
    if not isinstance(bones, list):
        bones = []
    writer.i32(len(bones))
    for bone in bones:
        write_pointer(writer, asset, bone)
    weights = fields.get("m_BlendShapeWeights")
    if not isinstance(weights, list):
        weights = []
    writer.i32(len(weights))
    for weight in weights:
        writer.f32(float(weight))
    write_pointer(writer, asset, fields.get("m_RootBone"))
    bounds = fields.get("m_AABB")
    if not isinstance(bounds, dict):
        bounds = {}
    write_vector(writer, bounds.get("m_Center"), 3, (0.0, 0.0, 0.0))
    write_vector(writer, bounds.get("m_Extent"), 3, (0.0, 0.0, 0.0))
    writer.bool(fields.get("m_DirtyAABB", False))
    writer.align()
    return writer.bytes()

def build_particle_system_renderer(fields: dict, asset: SerializedAsset) -> bytes:
    writer = EndianWriter(asset.endian)
    write_renderer_base(writer, fields, asset)
    if "m_RenderAlignment" in fields:
        writer.u16(int(fields.get("m_RenderMode", 0)))
        writer.u16(int(fields.get("m_SortMode", 0)))
        for name in (
            "m_MinParticleSize",
            "m_MaxParticleSize",
            "m_CameraVelocityScale",
            "m_VelocityScale",
            "m_LengthScale",
            "m_SortingFudge",
            "m_NormalDirection",
        ):
            writer.f32(float(fields.get(name, 0.0)))
        writer.i32(int(fields.get("m_RenderAlignment", 0)))
        write_vector(writer, fields.get("m_Pivot"), 3, (0.0, 0.0, 0.0))
        for name in ("m_Mesh", "m_Mesh1", "m_Mesh2", "m_Mesh3"):
            write_pointer(writer, asset, fields.get(name))
        return writer.bytes()
    writer.i32(int(fields.get("m_RenderMode", 0)))
    writer.i32(int(fields.get("m_SortMode", 0)))
    for name in (
        "m_MinParticleSize",
        "m_MaxParticleSize",
        "m_CameraVelocityScale",
        "m_VelocityScale",
        "m_LengthScale",
        "m_SortingFudge",
        "m_NormalDirection",
    ):
        writer.f32(float(fields.get(name, 0.0)))
    write_vector(writer, fields.get("m_Pivot"), 3, (0.0, 0.0, 0.0))
    for name in ("m_Mesh", "m_Mesh1", "m_Mesh2", "m_Mesh3"):
        write_pointer(writer, asset, fields.get(name))
    return writer.bytes()

def build_lod_group(fields: dict, asset: SerializedAsset) -> bytes:
    writer = EndianWriter(asset.endian)
    write_pointer(writer, asset, fields.get("m_GameObject"))
    write_vector(writer, fields.get("m_LocalReferencePoint"), 3, (0.0, 0.0, 0.0))
    writer.f32(float(fields.get("m_Size", 0.0)))
    writer.i32(int(fields.get("m_FadeMode", 0)))
    writer.bool(fields.get("m_AnimateCrossFading", False))
    writer.align()
    levels = fields.get("m_LODs")
    if not isinstance(levels, list):
        levels = []
    writer.i32(len(levels))
    for level in levels:
        level_data = level if isinstance(level, dict) else {}
        writer.f32(float(level_data.get("screenRelativeHeight", 0.0)))
        writer.f32(float(level_data.get("fadeTransitionWidth", 0.0)))
        renderers = level_data.get("renderers")
        if not isinstance(renderers, list):
            renderers = []
        writer.i32(len(renderers))
        for renderer in renderers:
            renderer_data = renderer if isinstance(renderer, dict) else {}
            write_pointer(writer, asset, renderer_data.get("renderer"))
    writer.bool(fields.get("m_Enabled", False))
    writer.align()
    return writer.bytes()

def build_reflection_probe(fields: dict, asset: SerializedAsset) -> bytes:
    writer = EndianWriter(asset.endian)
    write_pointer(writer, asset, fields.get("m_GameObject"))
    writer.bool(fields.get("m_Enabled", False))
    writer.u8(int(fields.get("m_Type", 0)))
    writer.u8(int(fields.get("m_Mode", 0)))
    writer.u8(int(fields.get("m_RefreshMode", 0)))
    writer.u8(int(fields.get("m_TimeSlicingMode", 0)))
    writer.align()
    write_pointer(writer, asset, fields.get("m_RuntimeTexture"))
    writer.i32(int(fields.get("m_Resolution", 0)))
    writer.i32(int(fields.get("m_UpdateFrequency", 0)))
    write_vector(writer, fields.get("m_BoxSize"), 3, (0.0, 0.0, 0.0))
    write_vector(writer, fields.get("m_BoxOffset"), 3, (0.0, 0.0, 0.0))
    writer.f32(float(fields.get("m_NearClip", 0.0)))
    writer.f32(float(fields.get("m_FarClip", 0.0)))
    writer.f32(float(fields.get("m_ShadowDistance", 0.0)))
    writer.i32(int(fields.get("m_ClearFlags", 0)))
    write_color(writer, fields.get("m_BackGroundColor"), (0.0, 0.0, 0.0, 0.0))
    writer.u32(int(fields.get("m_CullingMask", 0)))
    writer.f32(float(fields.get("m_IntensityMultiplier", 0.0)))
    writer.f32(float(fields.get("m_BlendDistance", 0.0)))
    writer.bool(fields.get("m_HDR", False))
    writer.bool(fields.get("m_BoxProjection", False))
    writer.bool(fields.get("m_RenderDynamicObjects", False))
    writer.bool(fields.get("m_UseOcclusionCulling", False))
    writer.i32(int(fields.get("m_Importance", 0)))
    write_pointer(writer, asset, fields.get("m_CustomBakedTexture"))
    write_pointer(writer, asset, fields.get("m_BakedTexture"))
    return writer.bytes()

def build_physic_material(fields: dict, asset: SerializedAsset) -> bytes:
    writer = EndianWriter(asset.endian)
    writer.aligned_string(fields.get("m_Name", ""))
    writer.f32(float(fields.get("dynamicFriction", 0.0)))
    writer.f32(float(fields.get("staticFriction", 0.0)))
    writer.f32(float(fields.get("bounciness", 0.0)))
    writer.i32(int(fields.get("frictionCombine", 0)))
    writer.i32(int(fields.get("bounceCombine", 0)))
    return writer.bytes()

def build_render_settings(fields: dict, asset: SerializedAsset) -> bytes:
    writer = EndianWriter(asset.endian)
    writer.bool(fields.get("m_Fog", False))
    writer.align()
    write_color(writer, fields.get("m_FogColor"), (0.0, 0.0, 0.0, 0.0))
    writer.i32(int(fields.get("m_FogMode", 0)))
    writer.f32(float(fields.get("m_FogDensity", 0.0)))
    writer.f32(float(fields.get("m_LinearFogStart", 0.0)))
    writer.f32(float(fields.get("m_LinearFogEnd", 0.0)))
    write_color(writer, fields.get("m_AmbientSkyColor"), (0.0, 0.0, 0.0, 0.0))
    write_color(writer, fields.get("m_AmbientEquatorColor"), (0.0, 0.0, 0.0, 0.0))
    write_color(writer, fields.get("m_AmbientGroundColor"), (0.0, 0.0, 0.0, 0.0))
    writer.f32(float(fields.get("m_AmbientIntensity", 0.0)))
    writer.i32(int(fields.get("m_AmbientMode", 0)))
    write_pointer(writer, asset, fields.get("m_SkyboxMaterial"))
    writer.f32(float(fields.get("m_HaloStrength", 0.0)))
    writer.f32(float(fields.get("m_FlareStrength", 0.0)))
    writer.f32(float(fields.get("m_FlareFadeSpeed", 0.0)))
    write_pointer(writer, asset, fields.get("m_HaloTexture"))
    write_pointer(writer, asset, fields.get("m_SpotCookie"))
    writer.i32(int(fields.get("m_DefaultReflectionMode", 0)))
    writer.i32(int(fields.get("m_DefaultReflectionResolution", 0)))
    writer.i32(int(fields.get("m_ReflectionBounces", 0)))
    writer.f32(float(fields.get("m_ReflectionIntensity", 0.0)))
    write_pointer(writer, asset, fields.get("m_CustomReflection"))
    probe = fields.get("m_AmbientProbe")
    if isinstance(probe, dict) and isinstance(probe.get("sh"), list):
        coefficients = probe["sh"]
        if len(coefficients) != 27:
            raise MapComponentRepackError("RenderSettings m_AmbientProbe.sh must hold 27 numbers")
        for value in coefficients:
            if isinstance(value, dict) and "f32Bits" in value:
                writer.u32(int(str(value["f32Bits"]), 16))
            else:
                writer.f32(float(value))
        write_pointer(writer, asset, fields.get("m_GeneratedSkyboxReflection"))
        write_pointer(writer, asset, fields.get("m_Sun"))
        write_color(writer, fields.get("m_IndirectSpecularColor"), (0.0, 0.0, 0.0, 0.0))
        return writer.bytes()
    write_pointer(writer, asset, fields.get("m_Sun"))
    ambient_probe = fields.get("m_AmbientProbeRaw")
    if not isinstance(ambient_probe, dict) or ambient_probe.get("encoding") != "hex":
        raise MapComponentRepackError("RenderSettings is missing m_AmbientProbeRaw hex data")
    probe_text = ambient_probe.get("data")
    if not isinstance(probe_text, str):
        raise MapComponentRepackError("RenderSettings ambient probe data must be hexadecimal text")
    try:
        probe_data = bytes.fromhex(probe_text)
    except ValueError as exc:
        raise MapComponentRepackError("RenderSettings ambient probe data is invalid") from exc
    if len(probe_data) != 120:
        raise MapComponentRepackError("RenderSettings ambient probe data must contain 120 bytes")
    writer.write(probe_data)
    write_color(writer, fields.get("m_IndirectSpecularColor"), (0.0, 0.0, 0.0, 0.0))
    return writer.bytes()

def write_renderer_base(
    writer: EndianWriter,
    fields: dict,
    asset: SerializedAsset,
) -> None:
    write_pointer(writer, asset, fields.get("m_GameObject"))
    writer.bool(fields.get("m_Enabled", False))
    writer.u8(int(fields.get("m_CastShadows", 0)))
    writer.bool(fields.get("m_ReceiveShadows", False))
    writer.u8(int(fields.get("m_MotionVectors", 0)))
    writer.u8(int(fields.get("m_LightProbeUsage", 0)))
    writer.u8(int(fields.get("m_ReflectionProbeUsage", 0)))
    writer.align()
    writer.u16(int(fields.get("m_LightmapIndex", 0)))
    writer.u16(int(fields.get("m_LightmapIndexDynamic", 0)))
    write_vector(writer, fields.get("m_LightmapTilingOffset"), 4, (0.0, 0.0, 0.0, 0.0))
    write_vector(
        writer,
        fields.get("m_LightmapTilingOffsetDynamic"),
        4,
        (0.0, 0.0, 0.0, 0.0),
    )
    materials = fields.get("m_Materials")
    if not isinstance(materials, list):
        materials = []
    writer.i32(len(materials))
    for material in materials:
        write_pointer(writer, asset, material)
    subset_indices = fields.get("m_SubsetIndices")
    if not isinstance(subset_indices, list):
        subset_indices = []
    writer.i32(len(subset_indices))
    for subset_index in subset_indices:
        writer.u32(int(subset_index))
    for name in ("m_StaticBatchRoot", "m_ProbeAnchor", "m_LightProbeVolumeOverride"):
        write_pointer(writer, asset, fields.get(name))
    writer.u32(int(fields.get("m_SortingLayerID", 0)))
    writer.i16(int(fields.get("m_SortingOrder", 0)))
    writer.align()

def patch_rigidbody(data: bytearray, fields: dict, asset: SerializedAsset) -> None:
    require_size(data, 36, "Rigidbody")
    patch_pointer(data, 0, fields.get("m_GameObject"), asset)
    patch_float(data, 12, fields.get("m_Mass"), asset)
    patch_float(data, 16, fields.get("m_Drag"), asset)
    patch_float(data, 20, fields.get("m_AngularDrag"), asset)
    patch_bool(data, 24, fields.get("m_UseGravity"))
    patch_bool(data, 25, fields.get("m_IsKinematic"))
    patch_byte(data, 26, fields.get("m_Interpolate"))
    patch_int(data, 28, fields.get("m_Constraints"), asset)
    patch_int(data, 32, fields.get("m_CollisionDetection"), asset)

def patch_mesh_collider(data: bytearray, fields: dict, asset: SerializedAsset) -> None:
    require_size(data, 44, "MeshCollider")
    patch_collider_base(data, fields, asset)
    patch_bool(data, 28, fields.get("m_Convex"))
    patch_pointer(data, 32, fields.get("m_Mesh"), asset)

def patch_box_collider(data: bytearray, fields: dict, asset: SerializedAsset) -> None:
    require_size(data, 52, "BoxCollider")
    patch_collider_base(data, fields, asset)
    patch_vector(data, 28, fields.get("m_Size"), 3, asset)
    patch_vector(data, 40, fields.get("m_Center"), 3, asset)

def patch_sphere_collider(data: bytearray, fields: dict, asset: SerializedAsset) -> None:
    require_size(data, 44, "SphereCollider")
    patch_collider_base(data, fields, asset)
    patch_float(data, 28, fields.get("m_Radius"), asset)
    patch_vector(data, 32, fields.get("m_Center"), 3, asset)

def patch_capsule_collider(data: bytearray, fields: dict, asset: SerializedAsset) -> None:
    require_size(data, 52, "CapsuleCollider")
    patch_collider_base(data, fields, asset)
    patch_float(data, 28, fields.get("m_Radius"), asset)
    patch_float(data, 32, fields.get("m_Height"), asset)
    patch_int(data, 36, fields.get("m_Direction"), asset)
    patch_vector(data, 40, fields.get("m_Center"), 3, asset)

def patch_character_controller(data: bytearray, fields: dict, asset: SerializedAsset) -> None:
    require_size(data, 64, "CharacterController")
    patch_collider_base(data, fields, asset)
    patch_float(data, 28, fields.get("m_Height"), asset)
    patch_float(data, 32, fields.get("m_Radius"), asset)
    patch_float(data, 36, fields.get("m_SlopeLimit"), asset)
    patch_float(data, 40, fields.get("m_StepOffset"), asset)
    patch_float(data, 44, fields.get("m_SkinWidth"), asset)
    patch_float(data, 48, fields.get("m_MinMoveDistance"), asset)
    patch_vector(data, 52, fields.get("m_Center"), 3, asset)

def patch_audio_reverb_zone(data: bytearray, fields: dict, asset: SerializedAsset) -> None:
    require_size(data, 84, "AudioReverbZone")
    patch_pointer(data, 0, fields.get("m_GameObject"), asset)
    patch_bool(data, 12, fields.get("m_Enabled"))
    patch_float(data, 16, fields.get("m_MinDistance"), asset)
    patch_float(data, 20, fields.get("m_MaxDistance"), asset)
    patch_int(data, 24, fields.get("m_ReverbPreset"), asset)
    patch_int(data, 28, fields.get("m_Room"), asset)
    patch_int(data, 32, fields.get("m_RoomHF"), asset)
    patch_float(data, 36, fields.get("m_DecayTime"), asset)
    patch_float(data, 40, fields.get("m_DecayHFRatio"), asset)
    patch_int(data, 44, fields.get("m_Reflections"), asset)
    patch_float(data, 48, fields.get("m_ReflectionsDelay"), asset)
    patch_int(data, 52, fields.get("m_Reverb"), asset)
    patch_float(data, 56, fields.get("m_ReverbDelay"), asset)
    patch_float(data, 60, fields.get("m_HFReference"), asset)
    patch_float(data, 64, fields.get("m_RoomRolloffFactor"), asset)
    patch_float(data, 68, fields.get("m_Diffusion"), asset)
    patch_float(data, 72, fields.get("m_Density"), asset)
    patch_float(data, 76, fields.get("m_LFReference"), asset)
    patch_int(data, 80, fields.get("m_RoomLF"), asset)

def patch_wind_zone(data: bytearray, fields: dict, asset: SerializedAsset) -> None:
    require_size(data, 40, "WindZone")
    patch_pointer(data, 0, fields.get("m_GameObject"), asset)
    patch_bool(data, 12, fields.get("m_Enabled"))
    patch_int(data, 16, fields.get("m_Mode"), asset)
    patch_float(data, 20, fields.get("m_Radius"), asset)
    patch_float(data, 24, fields.get("m_WindMain"), asset)
    patch_float(data, 28, fields.get("m_WindTurbulence"), asset)
    patch_float(data, 32, fields.get("m_WindPulseMagnitude"), asset)
    patch_float(data, 36, fields.get("m_WindPulseFrequency"), asset)

def patch_camera(data: bytearray, fields: dict, asset: SerializedAsset) -> None:
    require_size(data, 117, "Camera")
    patch_pointer(data, 0, fields.get("m_GameObject"), asset)
    patch_bool(data, 12, fields.get("m_Enabled"))
    patch_int(data, 16, fields.get("m_ClearFlags"), asset)
    patch_color(data, 20, fields.get("m_BackGroundColor"), asset)
    patch_rect(data, 36, fields.get("m_NormalizedViewPortRect"), asset)
    patch_float(data, 52, fields.get("near clip plane"), asset)
    patch_float(data, 56, fields.get("far clip plane"), asset)
    patch_float(data, 60, fields.get("field of view"), asset)
    patch_bool(data, 64, fields.get("orthographic"))
    patch_float(data, 68, fields.get("orthographic size"), asset)
    patch_float(data, 72, fields.get("m_Depth"), asset)
    patch_uint(data, 76, fields.get("m_CullingMask"), asset)
    patch_int(data, 80, fields.get("m_RenderingPath"), asset)
    patch_pointer(data, 84, fields.get("m_TargetTexture"), asset)
    patch_int(data, 96, fields.get("m_TargetDisplay"), asset)
    patch_int(data, 100, fields.get("m_TargetEye"), asset)
    patch_bool(data, 104, fields.get("m_HDR"))
    patch_bool(data, 105, fields.get("m_OcclusionCulling"))
    patch_float(data, 108, fields.get("m_StereoConvergence"), asset)
    patch_float(data, 112, fields.get("m_StereoSeparation"), asset)
    patch_bool(data, 116, fields.get("m_StereoMirrorMode"))

def patch_light(data: bytearray, fields: dict, asset: SerializedAsset) -> None:
    require_size(data, 136, "Light")
    patch_pointer(data, 0, fields.get("m_GameObject"), asset)
    patch_bool(data, 12, fields.get("m_Enabled"))
    patch_int(data, 16, fields.get("m_Type"), asset)
    patch_color(data, 20, fields.get("m_Color"), asset)
    patch_float(data, 36, fields.get("m_Intensity"), asset)
    patch_float(data, 40, fields.get("m_Range"), asset)
    patch_float(data, 44, fields.get("m_SpotAngle"), asset)
    patch_float(data, 48, fields.get("m_CookieSize"), asset)
    shadows = fields.get("m_Shadows")
    if isinstance(shadows, dict):
        patch_int(data, 52, shadows.get("m_Type"), asset)
        patch_int(data, 56, shadows.get("m_Resolution"), asset)
        patch_int(data, 60, shadows.get("m_CustomResolution"), asset)
        patch_float(data, 64, shadows.get("m_Strength"), asset)
        patch_float(data, 68, shadows.get("m_Bias"), asset)
        patch_float(data, 72, shadows.get("m_NormalBias"), asset)
        patch_float(data, 76, shadows.get("m_NearPlane"), asset)
    patch_pointer(data, 80, fields.get("m_Cookie"), asset)
    patch_bool(data, 92, fields.get("m_DrawHalo"))
    if "m_BakedIndex" in fields:
        patch_int(data, 96, fields.get("m_BakedIndex"), asset)
    else:
        patch_hex_bytes(data, 93, 7, fields.get("preservedBytes93To99"))
    patch_pointer(data, 100, fields.get("m_Flare"), asset)
    patch_int(data, 112, fields.get("m_RenderMode"), asset)
    patch_uint(data, 116, fields.get("m_CullingMask"), asset)
    patch_int(data, 120, fields.get("m_Lightmapping"), asset)
    patch_vector(data, 124, fields.get("m_AreaSize"), 2, asset)
    patch_float(data, 132, fields.get("m_BounceIntensity"), asset)

def patch_collider_base(data: bytearray, fields: dict, asset: SerializedAsset) -> None:
    patch_pointer(data, 0, fields.get("m_GameObject"), asset)
    patch_pointer(data, 12, fields.get("m_Material"), asset)
    patch_bool(data, 24, fields.get("m_IsTrigger"))
    patch_bool(data, 25, fields.get("m_Enabled"))

def patch_pointer(data: bytearray, offset: int, value: Any, asset: SerializedAsset) -> None:
    if not isinstance(value, dict):
        return
    file_id = value.get("fileId", value.get("m_FileID"))
    path_id = value.get("pathId", value.get("m_PathID"))
    if file_id is not None:
        struct.pack_into(asset.endian + "i", data, offset, int(file_id))
    if path_id is not None:
        format_code = "q" if asset.format_version >= 14 else "i"
        struct.pack_into(asset.endian + format_code, data, offset + 4, int(path_id))

def write_pointer(writer: EndianWriter, asset: SerializedAsset, value: Any) -> None:
    pointer = value if isinstance(value, dict) else {}
    writer.i32(int(pointer.get("fileId", pointer.get("m_FileID", 0))))
    path_id = int(pointer.get("pathId", pointer.get("m_PathID", 0)))
    if asset.format_version >= 14:
        writer.i64(path_id)
    else:
        writer.i32(path_id)

def write_vector(
    writer: EndianWriter,
    value: Any,
    length: int,
    defaults: tuple[float, ...],
) -> None:
    vector = value if isinstance(value, dict) else {}
    labels = ("x", "y", "z", "w")
    for index in range(length):
        writer.f32(float(vector.get(labels[index], defaults[index])))

def write_color(
    writer: EndianWriter,
    value: Any,
    defaults: tuple[float, float, float, float],
) -> None:
    color = value if isinstance(value, dict) else {}
    for index, label in enumerate(("r", "g", "b", "a")):
        writer.f32(float(color.get(label, defaults[index])))

def patch_vector(
    data: bytearray,
    offset: int,
    value: Any,
    length: int,
    asset: SerializedAsset,
) -> None:
    if not isinstance(value, dict):
        return
    labels = ("x", "y", "z", "w")
    for index in range(length):
        item = value.get(labels[index])
        if item is not None:
            patch_float(data, offset + index * 4, item, asset)

def patch_color(data: bytearray, offset: int, value: Any, asset: SerializedAsset) -> None:
    if not isinstance(value, dict):
        return
    for index, label in enumerate(("r", "g", "b", "a")):
        item = value.get(label)
        if item is not None:
            patch_float(data, offset + index * 4, item, asset)

def patch_rect(data: bytearray, offset: int, value: Any, asset: SerializedAsset) -> None:
    if not isinstance(value, dict):
        return
    for index, label in enumerate(("x", "y", "width", "height")):
        item = value.get(label)
        if item is not None:
            patch_float(data, offset + index * 4, item, asset)

def patch_bool(data: bytearray, offset: int, value: Any) -> None:
    if value is not None:
        data[offset] = 1 if bool(value) else 0

def patch_byte(data: bytearray, offset: int, value: Any) -> None:
    if value is not None:
        data[offset] = int(value) & 0xFF

def patch_hex_bytes(data: bytearray, offset: int, length: int, value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise MapComponentRepackError(
            f"opaque byte run at offset {offset} must be a hex string"
        )
    try:
        raw = bytes.fromhex(value.strip())
    except ValueError as exc:
        raise MapComponentRepackError(
            f"opaque byte run at offset {offset} isnt valid hex: {value[:24]!r}"
        ) from exc
    if len(raw) != length:
        raise MapComponentRepackError(
            f"opaque byte run at offset {offset} must decode to {length} bytes, got {len(raw)}"
        )
    data[offset : offset + length] = raw

def patch_int(data: bytearray, offset: int, value: Any, asset: SerializedAsset) -> None:
    if value is not None:
        struct.pack_into(asset.endian + "i", data, offset, int(value))

def patch_uint(data: bytearray, offset: int, value: Any, asset: SerializedAsset) -> None:
    if value is not None:
        struct.pack_into(asset.endian + "I", data, offset, int(value) & 0xFFFFFFFF)

def patch_float(data: bytearray, offset: int, value: Any, asset: SerializedAsset) -> None:
    if value is not None:
        struct.pack_into(asset.endian + "f", data, offset, float(value))

def require_size(data: bytearray, expected: int, label: str) -> None:
    if len(data) != expected:
        raise MapComponentRepackError(f"{label} expected {expected} bytes, found {len(data)}")
