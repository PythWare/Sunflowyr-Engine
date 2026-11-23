from __future__ import annotations
from typing import Any
from .model_context import ModelContextIndex
from .particle_system_export import decorate_pointers
from .serialized import SerializedAsset
from .unity_typetree_codec import load_schema, read_typetree, write_typetree

def parse_audio_source(
    data: bytes,
    asset: SerializedAsset,
    context: ModelContextIndex,
) -> dict[str, Any]:
    return parse_schema_component(data, asset, context, "audio_source_schema.json")

def build_audio_source(fields: dict[str, Any], asset: SerializedAsset) -> bytes:
    return build_schema_component(fields, asset, "audio_source_schema.json")

def parse_lightmap_settings(
    data: bytes,
    asset: SerializedAsset,
    context: ModelContextIndex,
) -> dict[str, Any]:
    return parse_schema_component(data, asset, context, "lightmap_settings_schema.json")

def build_lightmap_settings(fields: dict[str, Any], asset: SerializedAsset) -> bytes:
    return build_schema_component(fields, asset, "lightmap_settings_schema.json")

def parse_occlusion_culling_settings(
    data: bytes,
    asset: SerializedAsset,
    context: ModelContextIndex,
) -> dict[str, Any]:
    return read_typetree(
        data,
        load_schema("occlusion_culling_settings_schema.json"),
        asset.endian,
    )

def build_occlusion_culling_settings(
    fields: dict[str, Any],
    asset: SerializedAsset,
) -> bytes:
    return build_schema_component(
        fields,
        asset,
        "occlusion_culling_settings_schema.json",
    )

def parse_schema_component(
    data: bytes,
    asset: SerializedAsset,
    context: ModelContextIndex,
    schema_name: str,
) -> dict[str, Any]:
    fields = read_typetree(data, load_schema(schema_name), asset.endian)
    decorate_pointers(fields, asset, context)
    return fields

def build_schema_component(
    fields: dict[str, Any],
    asset: SerializedAsset,
    schema_name: str,
) -> bytes:
    return write_typetree(fields, load_schema(schema_name), asset.endian)
