from __future__ import annotations
import hashlib, json, re, sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from .animation_export import AnimationClipExportError, export_animation_clip_json
from .animation_glb_export import AnimationGlbExportError, export_animation_clip_glb
from .animation_package import AnimationPackageError, export_animation_package
from .audio_convert import AudioDecodeError, audio_view_extension, save_audio_view
from .binary import EndianReader, read_range
from .class_ids import DEFAULT_RAW_EXPORT_CLASS_IDS, class_name
from .cgprogram_export import CgProgramExportError, export_cgprogram_json
from .editable_object_export import (
    EDITABLE_ASSET_SCHEMAS,
    EDITABLE_COMPONENT_CLASS_IDS,
    EditableObjectExportError,
    export_editable_asset_json,
)
from .editable_table import TABLE_ASSET_CLASS_IDS, EditableTableError, export_editable_asset_table
from .font_export import FontExportError, export_font_view
from .material_export import MaterialExportError, export_material_json
from .map_component_export import MAP_COMPONENT_KINDS, MapComponentExportError, export_map_component_json
from .mesh_convert import MeshDecodeError
from .mesh_glb_export import export_mesh_glb
from .model_context import build_model_context
from .mono_export import (
    MonoExportError,
    build_monoscript_index,
    export_monobehaviour_json,
    export_monoscript_json,
)
from .objects import ResourcePayload, build_game_object_name_index, describe_object, read_mono_behaviour_base
from .raw_object_export import (
    CompleteRawObject,
    build_complete_raw_object,
)
from .reconstructed_export import export_reconstructed_glbs
from .serialized import SerializedAsset, SerializedObject, discover_serialized_files, load_serialized_asset
from .shader_package import ShaderPackageError, export_shader_package
from .sprite_export import SpriteExportContext, SpriteExportError, export_sprite_view
from .texture_convert import TextureDecodeError, save_png
from .texture_repack import SUPPORTED_TEXTURE_FORMATS
from .textmesh_export import TextMeshExportError, export_textmesh_json, read_textmesh_preview_name
from .ui_export import UI_CLASS_KINDS, UiExportError, export_ui_component_json
from . import export_log
from .paths import ensure_parent, reset as reset_directory_cache
from .parallel import ExportJob, JobRunner, describe_worker_choice, resolve_worker_count, seed_context

TAILDATA_FILENAME = "sunflowyr_taildata.json"
TAILDATA_FORMAT = "sunflowyr-taildata"

@dataclass(slots=True)
class ExportOptions:
    data_root: Path
    output_root: Path
    raw_objects: bool = False
    write_taildata: bool = True
    all_objects: bool = False
    include_classes: set[int] | None = None
    files: set[str] | None = None
    limit: int | None = None
    manifest: bool = False
    convert_png: bool = True
    convert_sprites: bool = True
    convert_audio: bool = True
    convert_text_assets: bool = True
    convert_model_glb: bool = True
    convert_reconstructed_glb: bool = True
    reconstructed_kinds: set[str] | None = None
    reconstructed_limit: int | None = None
    convert_mono_json: bool = True
    convert_animation_json: bool = False
    convert_animation_package: bool = True
    convert_animation_glb: bool = False
    convert_material_json: bool = True
    convert_fonts: bool = True
    convert_shaders: bool = True
    convert_textmesh_json: bool = True
    convert_ui_json: bool = True
    convert_map_component_json: bool = True
    convert_editable_asset_json: bool = True
    convert_gameobject_json: bool = False
    full_objects: bool = False
    jobs: int | None = None

@dataclass(slots=True)
class ExportStats:
    scanned_files: int = 0
    scanned_objects: int = 0
    raw_exports: int = 0
    raw_object_bytes: int = 0
    raw_inlined_resource_objects: int = 0
    raw_inlined_resource_bytes: int = 0
    raw_inlining_errors: int = 0
    png_exports: int = 0
    sprite_png_exports: int = 0
    sprite_json_exports: int = 0
    audio_exports: int = 0
    text_asset_exports: int = 0
    mesh_glb_exports: int = 0
    scene_glb_exports: int = 0
    prefab_glb_exports: int = 0
    scene_component_manifest_exports: int = 0
    prefab_component_manifest_exports: int = 0
    terrain_glb_exports: int = 0
    terrain_json_exports: int = 0
    navmesh_glb_exports: int = 0
    navmesh_json_exports: int = 0
    mono_json_exports: int = 0
    monoscript_json_exports: int = 0
    mono_json_typed_exports: int = 0
    mono_json_raw_fallbacks: int = 0
    media_settings_exports: int = 0
    animation_json_exports: int = 0
    animation_package_exports: int = 0
    animation_glb_exports: int = 0
    material_json_exports: int = 0
    font_json_exports: int = 0
    font_file_exports: int = 0
    shader_exports: int = 0
    textmesh_json_exports: int = 0
    ui_json_exports: int = 0
    map_component_json_exports: int = 0
    editable_asset_json_exports: int = 0
    unsupported_png: int = 0
    unsupported_sprites: int = 0
    unsupported_audio: int = 0
    unsupported_model_glb: int = 0
    reconstructed_glb_errors: int = 0
    unsupported_mono_json: int = 0
    unsupported_animation_json: int = 0
    unsupported_animation_package: int = 0
    unsupported_animation_glb: int = 0
    unsupported_material_json: int = 0
    unsupported_fonts: int = 0
    unsupported_shaders: int = 0
    unsupported_textmesh_json: int = 0
    unsupported_ui_json: int = 0
    unsupported_map_component_json: int = 0
    unsupported_editable_asset_json: int = 0
    skipped_missing_resources: int = 0
    errors: int = 0

def unpack(options: ExportOptions) -> ExportStats:
    options.data_root = options.data_root.resolve()
    options.output_root = options.output_root.resolve()
    options.output_root.mkdir(parents=True, exist_ok=True)
    reset_directory_cache()
    manifest_path = options.output_root / "manifest.jsonl"
    if manifest_path.exists():
        manifest_path.unlink()
    taildata_path = options.output_root / TAILDATA_FILENAME
    if options.write_taildata and taildata_path.exists():
        taildata_path.unlink()

    stats = ExportStats()
    export_log.begin(
        options.output_root,
        {"data root": options.data_root, "output root": options.output_root},
    )

    def note_failure(stage: str, error: BaseException, unsupported: bool = False) -> None:
        export_log.record(stage, export_log.source_label(asset, obj), error, unsupported)

    asset_paths = discover_serialized_files(options.data_root)
    needs_object_scan = options.raw_objects or has_editable_exports(options)
    assets = (
        [load_serialized_asset(path, options.data_root) for path in asset_paths]
        if needs_object_scan
        else []
    )
    taildata_records: dict[str, dict] = {}

    runner: JobRunner | None = None

    def finish_export() -> ExportStats:
        if runner is not None:
            runner.finish()
        if options.write_taildata and needs_object_scan:
            write_taildata_catalog(options, stats, assets, taildata_records)
        log_path = export_log.finish()
        if log_path is not None:
            failures, kinds = export_log.counts()
            print(f"{failures} object(s) failed or were unsupported, across {kinds} kind(s):")
            for line in export_log.summary_lines():
                print(line)
            print(f"full detail: {log_path}")
        return stats

    if not needs_object_scan:
        return finish_export()

    model_context = (
        build_model_context(assets)
        if options.convert_model_glb
        or options.convert_reconstructed_glb
        or options.convert_animation_package
        or options.convert_animation_glb
        or options.convert_map_component_json
        or options.convert_editable_asset_json
        or options.convert_gameobject_json
        else None
    )
    sprite_context = SpriteExportContext(assets) if options.convert_sprites else None
    mono_scripts = build_monoscript_index(assets) if options.convert_mono_json else {}
    managed_registry = build_managed_registry(options.data_root) if options.convert_mono_json else None
    if model_context is not None:
        game_object_names = model_context.game_object_names
    else:
        game_object_names = (
            build_game_object_name_index(assets)
            if options.convert_mono_json
            or options.convert_textmesh_json
            or options.convert_ui_json
            or options.raw_objects
            else {}
        )
    exported = 0
    reserved_output_paths: set[str] = set()

    workers = resolve_worker_count(options.jobs)
    if workers <= 1:
        seed_context(options.data_root, assets, model_context, mono_scripts, managed_registry)

    def on_job_complete(pending, result) -> None:
        if pending.kind == "reconstructed_batch":
            if result.status == "ok" and result.payload is not None:
                absorb_reconstructed(result.payload)
            else:
                stats.errors += 1
                stats.reconstructed_glb_errors += 1
                export_log.record(
                    "reconstructed_batch", pending.source, result.error or "no payload returned"
                )
            return
        if result.status == "ok":
            metadata = pending.metadata
            metadata.update(result.metadata)
            metadata.setdefault("export_size", result.export_size)
            for name in pending.success_stats:
                setattr(stats, name, getattr(stats, name) + 1)
            for name, amount in result.counters.items():
                setattr(stats, name, getattr(stats, name) + amount)
            record_taildata(taildata_records, options.output_root, pending.out_path, metadata)
            write_manifest(manifest_path, options.manifest, pending.out_path, metadata)
        elif result.status == "unsupported" and pending.unsupported_stat:
            setattr(stats, pending.unsupported_stat, getattr(stats, pending.unsupported_stat) + 1)
            export_log.record(pending.kind, pending.source, result.error, unsupported=True)
        else:
            stats.errors += 1
            export_log.record(pending.kind, pending.source, result.error or "unknown failure")

    def absorb_reconstructed(batch) -> None:
        stats.scene_glb_exports = batch.count("scene")
        stats.prefab_glb_exports = batch.count("prefab")
        stats.scene_component_manifest_exports = sum(
            result.kind == "scene" and result.component_manifest_path is not None for result in batch.results
        )
        stats.prefab_component_manifest_exports = sum(
            result.kind == "prefab" and result.component_manifest_path is not None for result in batch.results
        )
        stats.terrain_glb_exports = batch.count("terrain")
        stats.terrain_json_exports = sum(
            result.kind == "terrain" and result.terrain_json_path is not None for result in batch.results
        )
        stats.navmesh_glb_exports = batch.count("navmesh")
        stats.navmesh_json_exports = sum(
            result.kind == "navmesh" and result.navmesh_json_path is not None for result in batch.results
        )
        stats.reconstructed_glb_errors = batch.errors
        stats.errors += batch.errors
        for stage, source, message in batch.failures:
            export_log.record(stage, source, message)
        for result in batch.results:
            result.metadata["export_size"] = result.path.stat().st_size
            record_taildata(taildata_records, options.output_root, result.path, result.metadata)
            write_manifest(manifest_path, options.manifest, result.path, result.metadata)
            if result.component_manifest_path is not None and result.component_manifest_metadata is not None:
                result.component_manifest_metadata["export_size"] = result.component_manifest_path.stat().st_size
                record_taildata(
                    taildata_records,
                    options.output_root,
                    result.component_manifest_path,
                    result.component_manifest_metadata,
                )
                write_manifest(
                    manifest_path,
                    options.manifest,
                    result.component_manifest_path,
                    result.component_manifest_metadata,
                )
            if result.terrain_json_path is not None and result.terrain_json_metadata is not None:
                result.terrain_json_metadata["export_size"] = result.terrain_json_path.stat().st_size
                record_taildata(
                    taildata_records,
                    options.output_root,
                    result.terrain_json_path,
                    result.terrain_json_metadata,
                )
                write_manifest(
                    manifest_path,
                    options.manifest,
                    result.terrain_json_path,
                    result.terrain_json_metadata,
                )
            if result.navmesh_json_path is not None and result.navmesh_json_metadata is not None:
                result.navmesh_json_metadata["export_size"] = result.navmesh_json_path.stat().st_size
                record_taildata(
                    taildata_records,
                    options.output_root,
                    result.navmesh_json_path,
                    result.navmesh_json_metadata,
                )
                write_manifest(
                    manifest_path,
                    options.manifest,
                    result.navmesh_json_path,
                    result.navmesh_json_metadata,
                )
            for auxiliary_path, auxiliary_metadata in result.auxiliary_files:
                auxiliary_metadata["export_size"] = auxiliary_path.stat().st_size
                record_taildata(
                    taildata_records,
                    options.output_root,
                    auxiliary_path,
                    auxiliary_metadata,
                )
                write_manifest(
                    manifest_path,
                    options.manifest,
                    auxiliary_path,
                    auxiliary_metadata,
                )

    runner = JobRunner(options.data_root, workers, on_job_complete)

    if options.convert_reconstructed_glb and model_context is not None and runner.parallel:
        runner.submit(
            ExportJob(
                "reconstructed_batch",
                "",
                0,
                str(options.output_root),
                {
                    "output_root": str(options.output_root),
                    "kinds": sorted(options.reconstructed_kinds) if options.reconstructed_kinds else None,
                    "files": sorted(options.files) if options.files else None,
                    "limit": options.reconstructed_limit,
                },
            ),
            options.output_root,
            {},
            (),
            "",
        )

    for asset in assets:
        rel_name = asset.relative_path
        if options.files and rel_name.lower() not in options.files and asset.path.name.lower() not in options.files:
            continue
        stats.scanned_files += 1
        stats.scanned_objects += len(asset.objects)

        with asset.path.open("rb") as handle:
            reader = EndianReader(handle, asset.endian)
            for obj in asset.objects:
                if options.limit is not None and exported >= options.limit:
                    return finish_export()
                desc = describe_object(asset, reader, obj)
                if obj.class_id == 114:
                    desc.name = mono_behaviour_export_name(asset, reader, obj, desc.name, mono_scripts, game_object_names)
                should_export_raw = wants_raw_export(options, obj)
                if should_export_raw and options.raw_objects:
                    try:
                        out_path = reserve_output_path(
                            object_output_path(options.output_root, asset, obj, desc.name),
                            reserved_output_paths,
                            asset.relative_path,
                        )
                        metadata = object_taildata(asset, obj, desc.name, "raw_object")
                        exported += 1
                        runner.submit(
                            ExportJob("raw_object", asset.relative_path, obj.path_id, str(out_path)),
                            out_path,
                            metadata,
                            ("raw_exports",),
                            "",
                        )
                    except Exception as exc:
                        stats.errors += 1
                        note_failure("raw_object", exc)

                if options.convert_model_glb and obj.class_id == 43 and should_export_raw:
                    if options.limit is not None and exported >= options.limit:
                        return finish_export()
                    try:
                        mesh_glb_path = reserve_output_path(
                            mesh_glb_output_path(options.output_root, asset, obj, desc.name),
                            reserved_output_paths,
                            asset.relative_path,
                        )
                        metadata = object_taildata(asset, obj, desc.name, "mesh_glb")
                        exported += 1
                        write_object_settings(reader, asset, obj, mesh_glb_path, note_failure, stats)
                        runner.submit(
                            ExportJob("mesh_glb", asset.relative_path, obj.path_id, str(mesh_glb_path)),
                            mesh_glb_path,
                            metadata,
                            ("mesh_glb_exports",),
                            "unsupported_model_glb",
                        )
                    except Exception as exc:
                        stats.errors += 1
                        note_failure("mesh_glb", exc)

                if options.convert_mono_json and obj.class_id == 114 and should_export_raw:
                    if options.limit is not None and exported >= options.limit:
                        return finish_export()
                    try:
                        mono_path = reserve_output_path(
                            mono_json_output_path(options.output_root, asset, obj, desc.name),
                            reserved_output_paths,
                            asset.relative_path,
                        )
                        metadata = object_taildata(asset, obj, desc.name, "monobehaviour_json")
                        metadata["writeback_note"] = (
                            "MonoBehaviour JSON is a typed managed-field view when schema decoding succeeds, "
                            "raw serialized bytes are preserved on fallback exports."
                        )
                        exported += 1
                        runner.submit(
                            ExportJob("monobehaviour_json", asset.relative_path, obj.path_id, str(mono_path)),
                            mono_path,
                            metadata,
                            ("mono_json_exports",),
                            "unsupported_mono_json",
                        )
                    except Exception as exc:
                        stats.errors += 1
                        note_failure("monobehaviour_json", exc)

                if options.convert_mono_json and obj.class_id == 115 and should_export_raw:
                    if options.limit is not None and exported >= options.limit:
                        return finish_export()
                    try:
                        script_path = reserve_output_path(
                            monoscript_json_output_path(options.output_root, obj, desc.name),
                            reserved_output_paths,
                            asset.relative_path,
                        )
                        metadata = object_taildata(asset, obj, desc.name, "monoscript_json")
                        conversion = export_monoscript_json(
                            asset,
                            obj,
                            script_path,
                            metadata,
                        )
                        metadata["conversion"] = conversion
                        metadata["export_size"] = script_path.stat().st_size
                        stats.monoscript_json_exports += 1
                        exported += 1
                        record_taildata(taildata_records, options.output_root, script_path, metadata)
                        write_manifest(manifest_path, options.manifest, script_path, metadata)
                    except MonoExportError as exc:
                        stats.unsupported_mono_json += 1
                        note_failure("monoscript_json", exc, unsupported=True)
                    except Exception as exc:
                        stats.errors += 1
                        note_failure("monoscript_json", exc)

                if options.convert_animation_json and obj.class_id == 74 and should_export_raw:
                    if options.limit is not None and exported >= options.limit:
                        return finish_export()
                    try:
                        animation_path = reserve_output_path(
                            animation_json_output_path(options.output_root, asset, obj, desc.name),
                            reserved_output_paths,
                            asset.relative_path,
                        )
                        metadata = object_taildata(asset, obj, desc.name, "animation_clip_json")
                        metadata["writeback_note"] = (
                            "AnimationClip JSON is fully editable. The AssetBundle generator encodes it "
                            "straight back into a Unity AnimationClip object."
                        )
                        conversion = export_animation_clip_json(asset, obj, animation_path, metadata)
                        metadata["conversion"] = conversion
                        metadata["export_size"] = animation_path.stat().st_size
                        stats.animation_json_exports += 1
                        exported += 1
                        record_taildata(taildata_records, options.output_root, animation_path, metadata)
                        write_manifest(manifest_path, options.manifest, animation_path, metadata)
                    except AnimationClipExportError as exc:
                        stats.unsupported_animation_json += 1
                        note_failure("animation_clip_json", exc, unsupported=True)
                    except Exception as exc:
                        stats.errors += 1
                        note_failure("animation_clip_json", exc)

                if options.convert_animation_package and model_context is not None and obj.class_id == 74 and should_export_raw:
                    if options.limit is not None and exported >= options.limit:
                        return finish_export()
                    try:
                        animation_package_path = reserve_output_path(
                            animation_package_output_path(options.output_root, obj, desc.name),
                            reserved_output_paths,
                            asset.relative_path,
                        )
                        metadata = object_taildata(asset, obj, desc.name, "animation_clip_package")
                        exported += 1
                        runner.submit(
                            ExportJob(
                                "animation_package",
                                asset.relative_path,
                                obj.path_id,
                                str(animation_package_path),
                            ),
                            animation_package_path,
                            metadata,
                            ("animation_package_exports",),
                            "unsupported_animation_package",
                        )
                    except Exception as exc:
                        stats.errors += 1
                        note_failure("animation_package", exc)

                if options.convert_animation_glb and model_context is not None and obj.class_id == 74 and should_export_raw:
                    if options.limit is not None and exported >= options.limit:
                        return finish_export()
                    try:
                        animation_glb_path = reserve_output_path(
                            animation_glb_output_path(options.output_root, obj, desc.name),
                            reserved_output_paths,
                            asset.relative_path,
                        )
                        metadata = object_taildata(asset, obj, desc.name, "animation_clip_glb")
                        conversion = export_animation_clip_glb(
                            asset,
                            obj,
                            model_context,
                            animation_glb_path,
                            metadata,
                        )
                        metadata["conversion"] = conversion
                        metadata["export_size"] = animation_glb_path.stat().st_size
                        stats.animation_glb_exports += 1
                        exported += 1
                        record_taildata(taildata_records, options.output_root, animation_glb_path, metadata)
                        write_manifest(manifest_path, options.manifest, animation_glb_path, metadata)
                    except AnimationGlbExportError as exc:
                        stats.unsupported_animation_glb += 1
                        note_failure("animation_glb", exc, unsupported=True)
                    except Exception as exc:
                        stats.errors += 1
                        note_failure("animation_glb", exc)

                if options.convert_material_json and obj.class_id == 21 and should_export_raw:
                    if options.limit is not None and exported >= options.limit:
                        return finish_export()
                    try:
                        material_path = reserve_output_path(
                            material_json_output_path(options.output_root, asset, obj, desc.name),
                            reserved_output_paths,
                            asset.relative_path,
                        )
                        metadata = object_taildata(asset, obj, desc.name, "material_json")
                        metadata["writeback_note"] = (
                            "Material JSON is fully editable. writeback rebuilds the Unity Material from "
                            "this document. name, shader pointer, keywords, texture environments, floats "
                            "and colors, preserving only the unparsed tail."
                        )
                        conversion = export_material_json(asset, obj, material_path, metadata)
                        metadata["conversion"] = conversion
                        metadata["export_size"] = material_path.stat().st_size
                        stats.material_json_exports += 1
                        exported += 1
                        record_taildata(taildata_records, options.output_root, material_path, metadata)
                        write_manifest(manifest_path, options.manifest, material_path, metadata)
                    except MaterialExportError as exc:
                        stats.unsupported_material_json += 1
                        note_failure("material_json", exc, unsupported=True)
                    except Exception as exc:
                        stats.errors += 1
                        note_failure("material_json", exc)

                if options.convert_fonts and obj.class_id == 128 and should_export_raw:
                    if options.limit is not None and exported >= options.limit:
                        return finish_export()
                    try:
                        metadata = object_taildata(asset, obj, desc.name, "font_json")
                        result = export_font_view(
                            asset,
                            obj,
                            options.output_root,
                            reserve_path=lambda path, source: reserve_output_path(path, reserved_output_paths, source),
                            object_metadata=metadata,
                        )
                        result.json_metadata["export_size"] = result.json_path.stat().st_size
                        stats.font_json_exports += 1
                        exported += 1
                        record_taildata(taildata_records, options.output_root, result.json_path, result.json_metadata)
                        write_manifest(manifest_path, options.manifest, result.json_path, result.json_metadata)
                        if result.font_path is not None and result.font_metadata is not None:
                            result.font_metadata["export_size"] = result.font_path.stat().st_size
                            stats.font_file_exports += 1
                            exported += 1
                            record_taildata(taildata_records, options.output_root, result.font_path, result.font_metadata)
                            write_manifest(manifest_path, options.manifest, result.font_path, result.font_metadata)
                    except FontExportError as exc:
                        stats.unsupported_fonts += 1
                        note_failure("font", exc, unsupported=True)
                    except Exception as exc:
                        stats.errors += 1
                        note_failure("font", exc)

                if options.convert_shaders and obj.class_id == 48 and should_export_raw:
                    if options.limit is not None and exported >= options.limit:
                        return finish_export()
                    try:
                        shader_path = reserve_output_path(
                            shader_output_path(options.output_root, asset, obj, desc.name),
                            reserved_output_paths,
                            asset.relative_path,
                        )
                        metadata = object_taildata(asset, obj, desc.name, "shader_package")
                        conversion = export_shader_package(asset, obj, shader_path, metadata)
                        metadata["conversion"] = conversion
                        metadata["export_size"] = shader_path.stat().st_size
                        stats.shader_exports += 1
                        exported += 1
                        record_taildata(taildata_records, options.output_root, shader_path, metadata)
                        write_manifest(manifest_path, options.manifest, shader_path, metadata)
                    except ShaderPackageError as exc:
                        stats.unsupported_shaders += 1
                        note_failure("shader_package", exc, unsupported=True)
                    except Exception as exc:
                        stats.errors += 1
                        note_failure("shader_package", exc)

                if options.convert_shaders and obj.class_id == 109 and should_export_raw:
                    if options.limit is not None and exported >= options.limit:
                        return finish_export()
                    try:
                        program_path = reserve_output_path(
                            cgprogram_output_path(options.output_root, obj, desc.name),
                            reserved_output_paths,
                            asset.relative_path,
                        )
                        metadata = object_taildata(asset, obj, desc.name, "cgprogram_json")
                        conversion = export_cgprogram_json(asset, obj, program_path, metadata)
                        metadata["conversion"] = conversion
                        metadata["export_size"] = program_path.stat().st_size
                        stats.shader_exports += 1
                        exported += 1
                        record_taildata(taildata_records, options.output_root, program_path, metadata)
                        write_manifest(manifest_path, options.manifest, program_path, metadata)
                    except CgProgramExportError as exc:
                        stats.unsupported_shaders += 1
                        note_failure("cgprogram_json", exc, unsupported=True)
                    except Exception as exc:
                        stats.errors += 1
                        note_failure("cgprogram_json", exc)

                if options.convert_textmesh_json and obj.class_id == 102 and should_export_raw:
                    if options.limit is not None and exported >= options.limit:
                        return finish_export()
                    try:
                        preview_name = read_textmesh_preview_name(asset, obj)
                        textmesh_name = preview_name or desc.name
                        textmesh_path = reserve_output_path(
                            textmesh_json_output_path(options.output_root, asset, obj, textmesh_name),
                            reserved_output_paths,
                            asset.relative_path,
                        )
                        metadata = object_taildata(asset, obj, textmesh_name, "textmesh_json")
                        game_object_name = resolve_component_game_object_name(asset, obj, game_object_names, reader)
                        conversion = export_textmesh_json(asset, obj, textmesh_path, metadata, game_object_name)
                        metadata["conversion"] = conversion
                        metadata["export_size"] = textmesh_path.stat().st_size
                        stats.textmesh_json_exports += 1
                        exported += 1
                        record_taildata(taildata_records, options.output_root, textmesh_path, metadata)
                        write_manifest(manifest_path, options.manifest, textmesh_path, metadata)
                    except TextMeshExportError as exc:
                        stats.unsupported_textmesh_json += 1
                        note_failure("textmesh_json", exc, unsupported=True)
                    except Exception as exc:
                        stats.errors += 1
                        note_failure("textmesh_json", exc)

                if options.convert_ui_json and obj.class_id in UI_CLASS_KINDS and should_export_raw:
                    if options.limit is not None and exported >= options.limit:
                        return finish_export()
                    try:
                        game_object_name = resolve_component_game_object_name(asset, obj, game_object_names, reader)
                        ui_name = game_object_name or obj.type_name
                        ui_path = reserve_output_path(
                            ui_json_output_path(options.output_root, obj, ui_name),
                            reserved_output_paths,
                            asset.relative_path,
                        )
                        metadata = object_taildata(asset, obj, ui_name, UI_CLASS_KINDS[obj.class_id])
                        conversion = export_ui_component_json(asset, obj, ui_path, metadata, game_object_name)
                        metadata["conversion"] = conversion
                        metadata["export_size"] = ui_path.stat().st_size
                        stats.ui_json_exports += 1
                        exported += 1
                        record_taildata(taildata_records, options.output_root, ui_path, metadata)
                        write_manifest(manifest_path, options.manifest, ui_path, metadata)
                    except UiExportError as exc:
                        stats.unsupported_ui_json += 1
                        note_failure("ui_json", exc, unsupported=True)
                    except Exception as exc:
                        stats.errors += 1
                        note_failure("ui_json", exc)

                map_component_selected = options.include_classes is None or obj.class_id in options.include_classes
                if (
                    options.convert_map_component_json
                    and model_context is not None
                    and obj.class_id in MAP_COMPONENT_KINDS
                    and map_component_selected
                ):
                    if options.limit is not None and exported >= options.limit:
                        return finish_export()
                    try:
                        game_object_name = resolve_component_game_object_name(asset, obj, game_object_names, reader)
                        component_name = game_object_name or obj.type_name
                        component_path = reserve_output_path(
                            map_component_json_output_path(
                                options.output_root,
                                obj,
                                component_name,
                            ),
                            reserved_output_paths,
                            asset.relative_path,
                        )
                        metadata = object_taildata(
                            asset,
                            obj,
                            component_name,
                            MAP_COMPONENT_KINDS[obj.class_id],
                        )
                        exported += 1
                        runner.submit(
                            ExportJob(
                                "map_component_json",
                                asset.relative_path,
                                obj.path_id,
                                str(component_path),
                            ),
                            component_path,
                            metadata,
                            ("map_component_json_exports",),
                            "unsupported_map_component_json",
                        )
                    except Exception as exc:
                        stats.errors += 1
                        note_failure("map_component_json", exc)

                editable_asset_selected = options.include_classes is None or obj.class_id in options.include_classes
                editable_asset_enabled = (
                    options.convert_gameobject_json
                    if obj.class_id == 1
                    else options.convert_editable_asset_json
                )
                if (
                    editable_asset_enabled
                    and model_context is not None
                    and obj.class_id in EDITABLE_ASSET_SCHEMAS
                    and editable_asset_selected
                ):
                    if options.limit is not None and exported >= options.limit:
                        return finish_export()
                    try:
                        component_name = (
                            resolve_component_game_object_name(
                                asset,
                                obj,
                                game_object_names,
                                reader,
                            )
                            if obj.class_id in EDITABLE_COMPONENT_CLASS_IDS
                            else ""
                        )
                        asset_name = desc.name or component_name or obj.type_name
                        asset_path = reserve_output_path(
                            editable_asset_json_output_path(
                                options.output_root,
                                obj,
                                asset_name,
                            ),
                            reserved_output_paths,
                            asset.relative_path,
                        )
                        asset_kind = EDITABLE_ASSET_SCHEMAS[obj.class_id][0]
                        metadata = object_taildata(
                            asset,
                            obj,
                            asset_name,
                            asset_kind,
                        )
                        exported += 1
                        runner.submit(
                            ExportJob(
                                "editable_asset_table"
                                if obj.class_id in TABLE_ASSET_CLASS_IDS
                                else "editable_asset_json",
                                asset.relative_path,
                                obj.path_id,
                                str(asset_path),
                                {"asset_name": asset_name},
                            ),
                            asset_path,
                            metadata,
                            ("editable_asset_json_exports",),
                            "unsupported_editable_asset_json",
                        )
                    except Exception as exc:
                        stats.errors += 1
                        note_failure("editable_asset_json", exc)

                if options.convert_sprites and sprite_context is not None and obj.class_id == 213 and should_export_raw:
                    if options.limit is not None and exported >= options.limit:
                        return finish_export()
                    try:
                        sprite_metadata = object_taildata(asset, obj, desc.name, "sprite")
                        result = export_sprite_view(
                            sprite_context,
                            asset,
                            obj,
                            options.output_root,
                            write_embedded_taildata=False,
                            reserve_path=lambda path, source: reserve_output_path(path, reserved_output_paths, source),
                            object_metadata=sprite_metadata,
                        )
                        stats.sprite_png_exports += 1
                        stats.sprite_json_exports += 1
                        exported += 2
                        result.png_metadata["export_size"] = result.png_path.stat().st_size
                        result.json_metadata["export_size"] = result.json_path.stat().st_size
                        record_taildata(taildata_records, options.output_root, result.png_path, result.png_metadata)
                        record_taildata(taildata_records, options.output_root, result.json_path, result.json_metadata)
                        write_manifest(manifest_path, options.manifest, result.png_path, result.png_metadata)
                        write_manifest(manifest_path, options.manifest, result.json_path, result.json_metadata)
                    except (SpriteExportError, TextureDecodeError) as exc:
                        stats.unsupported_sprites += 1
                        note_failure("sprite", exc, unsupported=True)
                    except Exception as exc:
                        stats.errors += 1
                        note_failure("sprite", exc)

                if options.convert_text_assets or options.convert_png or options.convert_audio:
                    if options.include_classes is not None and obj.class_id not in options.include_classes:
                        continue
                    for payload in desc.payloads:
                        if options.limit is not None and exported >= options.limit:
                            return finish_export()
                        try:
                            resource_path = resolve_resource(asset, payload)
                            if resource_path is None:
                                stats.skipped_missing_resources += 1
                                continue
                            metadata = payload_taildata(asset, obj, payload, resource_path)

                            if options.convert_text_assets and payload.kind == "text_asset":
                                if options.limit is not None and exported >= options.limit:
                                    return finish_export()
                                text_path = reserve_output_path(
                                    text_asset_output_path(options.output_root, asset, obj, payload),
                                    reserved_output_paths,
                                    asset.relative_path,
                                )
                                text_metadata = dict(metadata)
                                text_metadata["conversion"] = {
                                    "format": "text_asset_bytes",
                                    "byteLength": payload.source_size,
                                }
                                copy_export_bytes(
                                    source_path=resource_path,
                                    source_offset=payload.source_offset,
                                    source_size=payload.source_size,
                                    out_path=text_path,
                                )
                                text_metadata["export_size"] = text_path.stat().st_size
                                stats.text_asset_exports += 1
                                exported += 1
                                record_taildata(
                                    taildata_records,
                                    options.output_root,
                                    text_path,
                                    text_metadata,
                                )
                                write_manifest(
                                    manifest_path,
                                    options.manifest,
                                    text_path,
                                    text_metadata,
                                )

                            if options.convert_png and payload.kind in {"texture2d", "cubemap"}:
                                if options.limit is not None and exported >= options.limit:
                                    return finish_export()
                                try:
                                    png_path = reserve_output_path(
                                        png_output_path(options.output_root, asset, obj, payload),
                                        reserved_output_paths,
                                        asset.relative_path,
                                    )
                                    png_metadata = png_taildata(metadata, payload)
                                    exported += 1
                                    write_object_settings(reader, asset, obj, png_path, note_failure, stats)
                                    runner.submit(
                                        ExportJob(
                                            "png",
                                            asset.relative_path,
                                            obj.path_id,
                                            str(png_path),
                                            {
                                                "resource": str(resource_path),
                                                "offset": payload.source_offset,
                                                "size": payload.source_size,
                                                "width": int(payload.details["width"]),
                                                "height": int(payload.details["height"]),
                                                "texture_format": int(payload.details["texture_format"]),
                                                "flip_vertical": payload.kind != "cubemap",
                                                "image_count": int(payload.details.get("image_count", 1)),
                                                "complete_image_size": int(
                                                    payload.details.get("complete_image_size", 0)
                                                ),
                                            },
                                        ),
                                        png_path,
                                        png_metadata,
                                        ("png_exports",),
                                        "unsupported_png",
                                    )
                                except Exception as exc:
                                    stats.errors += 1
                                    note_failure("png", exc)

                            if options.convert_audio and payload.kind == "audio_clip":
                                if options.limit is not None and exported >= options.limit:
                                    return finish_export()
                                try:
                                    payload_bytes = b"".join(
                                        read_range(resource_path, payload.source_offset, payload.source_size)
                                    )
                                    audio_extension = audio_view_extension(payload_bytes, payload.details)
                                    audio_path = reserve_output_path(
                                        audio_output_path(options.output_root, asset, obj, payload, audio_extension),
                                        reserved_output_paths,
                                        asset.relative_path,
                                    )
                                    audio_metadata = audio_taildata(metadata, payload, audio_extension)
                                    exported += 1
                                    write_object_settings(reader, asset, obj, audio_path, note_failure, stats)
                                    runner.submit(
                                        ExportJob(
                                            "audio",
                                            asset.relative_path,
                                            obj.path_id,
                                            str(audio_path),
                                            {
                                                "resource": str(resource_path),
                                                "offset": payload.source_offset,
                                                "size": payload.source_size,
                                                "details": dict(payload.details),
                                            },
                                        ),
                                        audio_path,
                                        audio_metadata,
                                        ("audio_exports",),
                                        "unsupported_audio",
                                    )
                                except Exception as exc:
                                    stats.errors += 1
                                    note_failure("audio", exc)
                        except Exception as exc:
                            stats.errors += 1
                            note_failure("resource_payload", exc)

    if options.convert_reconstructed_glb and model_context is not None and not runner.parallel:
        absorb_reconstructed(
            export_reconstructed_glbs(
                model_context,
                options.output_root,
                embed_taildata=False,
                reserve_path=lambda path, source: reserve_output_path(
                    path, reserved_output_paths, source
                ),
                kinds=options.reconstructed_kinds,
                files=options.files,
                limit=options.reconstructed_limit,
            )
        )

    runner.finish()
    return finish_export()

def write_object_settings(
    reader: EndianReader,
    asset: SerializedAsset,
    obj: SerializedObject,
    media_path: Path,
    note_failure: Callable[..., None],
    stats: ExportStats,
) -> None:
    from .media_settings import write_media_settings

    try:
        reader.seek(obj.byte_start)
        data = reader.read(obj.byte_size)
        media_path.parent.mkdir(parents=True, exist_ok=True)
        write_media_settings(data, obj.class_id, asset.endian, media_path)
        stats.media_settings_exports += 1
    except Exception as exc:
        stats.errors += 1
        note_failure("media_settings", exc)

def build_managed_registry(data_root: Path) -> Any | None:
    try:
        from .managed_schema import shared_registry
    except Exception:
        return None
    registry = shared_registry(data_root / "Managed")
    return registry

def mono_behaviour_export_name(
    asset: SerializedAsset,
    reader: EndianReader,
    obj: SerializedObject,
    current_name: str,
    mono_scripts: dict,
    game_object_names: dict[tuple[str, int], str],
) -> str:
    if current_name:
        return current_name
    try:
        game_object, script, name = read_mono_behaviour_base(asset, reader, obj)
    except Exception:
        return current_name

    script_info = resolve_mono_script_info(asset, script, mono_scripts)
    if script_info is not None:
        return script_info.name or script_info.class_name or script_info.full_name

    game_object_name = resolve_game_object_name(asset, game_object, game_object_names)
    return game_object_name or current_name

def resolve_mono_script_info(asset: SerializedAsset, script: dict[str, int], mono_scripts: dict) -> Any | None:
    path_id = int(script["path_id"])
    if path_id == 0:
        return None
    file_id = int(script["file_id"])
    if file_id == 0:
        return mono_scripts.get((norm_asset_key(asset.relative_path), path_id))
    external_index = file_id - 1
    if 0 <= external_index < len(asset.externals):
        external = asset.externals[external_index]
        return mono_scripts.get((norm_asset_key(Path(external).name), path_id)) or mono_scripts.get(
            (norm_asset_key(external), path_id)
        )
    return None

def resolve_game_object_name(
    asset: SerializedAsset,
    game_object: dict[str, int],
    game_object_names: dict[tuple[str, int], str],
) -> str:
    path_id = int(game_object["path_id"])
    if path_id == 0:
        return ""
    file_id = int(game_object["file_id"])
    if file_id == 0:
        return game_object_names.get((norm_asset_key(asset.relative_path), path_id), "")
    external_index = file_id - 1
    if 0 <= external_index < len(asset.externals):
        external = asset.externals[external_index]
        return game_object_names.get((norm_asset_key(Path(external).name), path_id), "") or game_object_names.get(
            (norm_asset_key(external), path_id), ""
        )
    return ""

def resolve_component_game_object_name(
    asset: SerializedAsset,
    obj: SerializedObject,
    game_object_names: dict[tuple[str, int], str],
    reader: EndianReader | None = None,
) -> str:
    try:
        if reader is None:
            with asset.path.open("rb") as handle:
                opened = EndianReader(handle, asset.endian)
                opened.seek(obj.byte_start)
                file_id = opened.i32()
                path_id = opened.i64() if asset.format_version >= 14 else opened.i32()
        else:
            reader.seek(obj.byte_start)
            file_id = reader.i32()
            path_id = reader.i64() if asset.format_version >= 14 else reader.i32()
    except Exception:
        return ""
    return resolve_game_object_name(asset, {"file_id": file_id, "path_id": path_id}, game_object_names)

def norm_asset_key(path: str) -> str:
    return path.replace("\\", "/").lower()

def inventory(data_root: Path) -> dict:
    data_root = data_root.resolve()
    files = []
    total_objects = 0
    total_classes: dict[str, int] = {}
    for path in discover_serialized_files(data_root):
        asset = load_serialized_asset(path, data_root)
        total_objects += len(asset.objects)
        counts = {class_label(cid): count for cid, count in asset.class_counts().most_common()}
        for name, count in counts.items():
            total_classes[name] = total_classes.get(name, 0) + count
        files.append(
            {
                "file": asset.relative_path,
                "unity_version": asset.unity_version,
                "format_version": asset.format_version,
                "objects": len(asset.objects),
                "type_tree": asset.enable_type_tree,
                "classes": counts,
            }
        )
    return {
        "data_root": str(data_root),
        "files": files,
        "file_count": len(files),
        "object_count": total_objects,
        "classes": dict(sorted(total_classes.items(), key=lambda item: item[1], reverse=True)),
    }

def class_label(class_id: int) -> str:
    return f"{class_id:03d}_{class_name(class_id)}"

def wants_raw_export(options: ExportOptions, obj: SerializedObject) -> bool:
    if options.include_classes is not None:
        return obj.class_id in options.include_classes
    if options.raw_objects:
        return True
    if options.full_objects:
        return True
    if options.all_objects:
        return True
    return obj.class_id in DEFAULT_RAW_EXPORT_CLASS_IDS

def has_editable_exports(options: ExportOptions) -> bool:
    return any(
        (
            options.convert_png,
            options.convert_sprites,
            options.convert_audio,
            options.convert_text_assets,
            options.convert_model_glb,
            options.convert_reconstructed_glb,
            options.convert_mono_json,
            options.convert_animation_json,
            options.convert_animation_package,
            options.convert_animation_glb,
            options.convert_material_json,
            options.convert_fonts,
            options.convert_shaders,
            options.convert_textmesh_json,
            options.convert_ui_json,
            options.convert_map_component_json,
            options.convert_editable_asset_json,
            options.convert_gameobject_json,
        )
    )

def copy_export_bytes(
    source_path: Path,
    source_offset: int,
    source_size: int,
    out_path: Path,
) -> None:
    ensure_parent(out_path)
    with out_path.open("wb") as out:
        for chunk in read_range(source_path, source_offset, source_size):
            out.write(chunk)

def write_export_data(data: bytes, out_path: Path) -> None:
    ensure_parent(out_path)
    out_path.write_bytes(data)

def record_taildata(records: dict[str, dict], output_root: Path, out_path: Path, metadata: dict) -> None:
    try:
        relative_output = out_path.relative_to(output_root).as_posix()
    except ValueError:
        try:
            relative_output = out_path.resolve().relative_to(output_root.resolve()).as_posix()
        except ValueError:
            relative_output = out_path.name
    record = camelize_taildata(metadata)
    record["outputPath"] = relative_output
    records[relative_output] = record

def write_taildata_catalog(
    options: ExportOptions,
    stats: ExportStats,
    assets: list[SerializedAsset],
    records: dict[str, dict],
) -> None:
    unity_versions = sorted({asset.unity_version for asset in assets if asset.unity_version})
    catalog = {
        "format": TAILDATA_FORMAT,
        "version": 1,
        "game": "rwby-ge",
        "tool": "Sunflowyr Engine",
        "createdUtc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataRoot": str(options.data_root),
        "outputRoot": str(options.output_root),
        "unityVersions": unity_versions,
        "taildataMode": "external-json",
        "taildataNote": (
            "External metadata for clean unpacked/converted files. Keys are output paths relative to this folder. "
            "Converted formats use conversion records to map view files back to Unity source data."
        ),
        "stats": {
            "scannedFiles": stats.scanned_files,
            "scannedObjects": stats.scanned_objects,
            "rawExports": stats.raw_exports,
            "rawObjectBytes": stats.raw_object_bytes,
            "rawInlinedResourceObjects": stats.raw_inlined_resource_objects,
            "rawInlinedResourceBytes": stats.raw_inlined_resource_bytes,
            "rawInliningErrors": stats.raw_inlining_errors,
            "pngExports": stats.png_exports,
            "spritePngExports": stats.sprite_png_exports,
            "spriteJsonExports": stats.sprite_json_exports,
            "audioExports": stats.audio_exports,
            "textAssetExports": stats.text_asset_exports,
            "meshGlbExports": stats.mesh_glb_exports,
            "sceneGlbExports": stats.scene_glb_exports,
            "prefabGlbExports": stats.prefab_glb_exports,
            "sceneComponentManifestExports": stats.scene_component_manifest_exports,
            "prefabComponentManifestExports": stats.prefab_component_manifest_exports,
            "terrainGlbExports": stats.terrain_glb_exports,
            "terrainJsonExports": stats.terrain_json_exports,
            "navmeshGlbExports": stats.navmesh_glb_exports,
            "navmeshJsonExports": stats.navmesh_json_exports,
            "monoJsonExports": stats.mono_json_exports,
            "monoScriptJsonExports": stats.monoscript_json_exports,
            "animationJsonExports": stats.animation_json_exports,
            "animationPackageExports": stats.animation_package_exports,
            "animationGlbExports": stats.animation_glb_exports,
            "materialJsonExports": stats.material_json_exports,
            "fontJsonExports": stats.font_json_exports,
            "fontFileExports": stats.font_file_exports,
            "shaderExports": stats.shader_exports,
            "textMeshJsonExports": stats.textmesh_json_exports,
            "uiJsonExports": stats.ui_json_exports,
            "mapComponentJsonExports": stats.map_component_json_exports,
            "editableAssetJsonExports": stats.editable_asset_json_exports,
            "unsupported": {
                "png": stats.unsupported_png,
                "sprites": stats.unsupported_sprites,
                "audio": stats.unsupported_audio,
                "modelGlb": stats.unsupported_model_glb,
                "monoJson": stats.unsupported_mono_json,
                "animationJson": stats.unsupported_animation_json,
                "animationPackage": stats.unsupported_animation_package,
                "animationGlb": stats.unsupported_animation_glb,
                "materialJson": stats.unsupported_material_json,
                "fonts": stats.unsupported_fonts,
                "shaders": stats.unsupported_shaders,
                "textMeshJson": stats.unsupported_textmesh_json,
                "uiJson": stats.unsupported_ui_json,
                "mapComponentJson": stats.unsupported_map_component_json,
                "editableAssetJson": stats.unsupported_editable_asset_json,
            },
            "unsupportedTotal": (
                stats.unsupported_png
                + stats.unsupported_sprites
                + stats.unsupported_audio
                + stats.unsupported_model_glb
                + stats.unsupported_mono_json
                + stats.unsupported_animation_json
                + stats.unsupported_animation_package
                + stats.unsupported_animation_glb
                + stats.unsupported_material_json
                + stats.unsupported_fonts
                + stats.unsupported_shaders
                + stats.unsupported_textmesh_json
                + stats.unsupported_ui_json
                + stats.unsupported_map_component_json
                + stats.unsupported_editable_asset_json
            ),
        },
        "files": dict(sorted(records.items())),
    }
    path = options.output_root / TAILDATA_FILENAME
    text = json.dumps(catalog, ensure_ascii=False, indent=2)
    path.write_text(text, encoding="utf-8")

def camelize_taildata(value: Any) -> Any:
    if isinstance(value, dict):
        return {snake_to_camel(str(key)): camelize_taildata(item) for key, item in value.items()}
    if isinstance(value, list):
        return [camelize_taildata(item) for item in value]
    return value

CAMEL_CACHE: dict[str, str] = {}

def snake_to_camel(name: str) -> str:
    cached = CAMEL_CACHE.get(name)
    if cached is not None:
        return cached
    if not name or name.startswith("$"):
        CAMEL_CACHE[name] = name
        return name
    parts = name.split("_")
    if not parts:
        CAMEL_CACHE[name] = name
        return name
    value = parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:] if part)
    CAMEL_CACHE[name] = value
    return value

def object_taildata(asset: SerializedAsset, obj: SerializedObject, name: str, kind: str) -> dict:
    return {
        "sunflowyrTaildata": 1,
        "kind": kind,
        "unity_version": asset.unity_version,
        "serialized_format_version": asset.format_version,
        "endian": "little" if asset.endian == "<" else "big",
        "container": asset.relative_path,
        "append_target": asset.relative_path,
        "path_id": obj.path_id,
        "class_id": obj.class_id,
        "class_name": obj.type_name,
        "name": name,
        "object_entry_offset": obj.entry_offset,
        "path_id_offset": obj.path_id_offset,
        "byte_start_field_offset": obj.byte_start_field_offset,
        "byte_size_field_offset": obj.byte_size_field_offset,
        "byte_start_relative": obj.byte_start - asset.data_offset,
        "byte_start_absolute": obj.byte_start,
        "byte_size": obj.byte_size,
        "data_offset": asset.data_offset,
        "field_widths": {
            "byte_start": 4 if asset.format_version < 22 else 8,
            "byte_size": 4,
        },
    }

def payload_taildata(
    asset: SerializedAsset,
    obj: SerializedObject,
    payload: ResourcePayload,
    resource_path: Path,
) -> dict:
    metadata = object_taildata(asset, obj, payload.name, payload.kind)
    metadata.update(
        {
            "resource_container": rel(resource_path, asset.data_root),
            "resource_offset": payload.source_offset,
            "resource_size": payload.source_size,
            "resource_offset_field_offset": payload.offset_field_offset,
            "resource_size_field_offset": payload.size_field_offset,
            "resource_details": payload.details,
        }
    )
    return metadata

def png_taildata(payload_metadata: dict, payload: ResourcePayload) -> dict:
    metadata = dict(payload_metadata)
    texture_format = int(payload.details.get("texture_format", 0))
    writeback_supported = texture_format in SUPPORTED_TEXTURE_FORMATS
    if writeback_supported:
        note = (
            "PNG is editable at any resolution up to 16384 per side. The AssetBundle generator rebuilds it into "
            "an inline Unity Texture2D/Cubemap in the original texture format and regenerates the mip chain. "
            "DXT formats need sides that are multiples of 4 and Cubemap faces must stay square powers of two. "
            "A crunched texture whose new image needs more than Crunch's 8192 entry codebooks is written as "
            "plain DXT1/DXT5 instead."
        )
    else:
        note = (
            "PNG is a view only. RWBY GE doesnt ship this texture format, so Sunflowyr has no "
            "encoder to write it back with."
        )
    metadata.update(
        {
            "kind": "texture_png",
            "view_of_kind": payload.kind,
            "texture_writeback_supported": writeback_supported,
            "texture_writeback_format": "texture_png_to_unity_object" if writeback_supported else "",
            "writeback_note": note,
        }
    )
    return metadata

def audio_taildata(payload_metadata: dict, payload: ResourcePayload, extension: str) -> dict:
    metadata = dict(payload_metadata)
    if extension == ".ogg":
        metadata.update(
            {
                "kind": "audio_ogg",
                "audio_writeback_format": "audio_ogg_to_fsb5_vorbis",
                "writeback_note": (
                    "The AssetBundle generator rebuilds this into an inline FSB5 Vorbis AudioClip when the "
                    "edited OGG uses one of the Vorbis setups FMOD ships with RWBY GE (standard libvorbis "
                    "encodes normally do). Otherwise save the edit as PCM WAV under the same name and it "
                    "is rebuilt as an FSB5 PCM AudioClip."
                ),
            }
        )
    else:
        metadata.update(
            {
                "kind": "audio_wav",
                "audio_writeback_format": "audio_wav_to_fsb5_pcm",
                "writeback_note": (
                    "The AssetBundle generator rebuilds PCM WAV edits into inline FSB5 PCM AudioClips."
                ),
            }
        )
    metadata.update({"view_of_kind": payload.kind, "audio_writeback_supported": True})
    return metadata

def write_manifest(manifest_path: Path, enabled: bool, out_path: Path, metadata: dict) -> None:
    if not enabled:
        return
    row = {"output": str(out_path), **metadata}
    with manifest_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def object_output_path(output_root: Path, asset: SerializedAsset, obj: SerializedObject, name: str) -> Path:
    class_dir = f"{obj.class_id:03d}_{safe_name(obj.type_name)}"
    file_name = f"{safe_name(name) or obj.type_name}__path{obj.path_id}.uobj"
    return output_root / "raw_objects" / class_dir / file_name

def text_asset_output_path(
    output_root: Path,
    asset: SerializedAsset,
    obj: SerializedObject,
    payload: ResourcePayload,
) -> Path:
    class_dir = f"{obj.class_id:03d}_{safe_name(obj.type_name)}"
    file_name = f"{safe_name(payload.name) or obj.type_name}__path{obj.path_id}{payload.extension}"
    return output_root / "converted" / "text_asset" / class_dir / file_name

def png_output_path(output_root: Path, asset: SerializedAsset, obj: SerializedObject, payload: ResourcePayload) -> Path:
    class_dir = f"{obj.class_id:03d}_{safe_name(obj.type_name)}"
    file_name = f"{safe_name(payload.name) or obj.type_name}__path{obj.path_id}.png"
    return output_root / "converted" / "png" / class_dir / file_name

def audio_output_path(
    output_root: Path,
    asset: SerializedAsset,
    obj: SerializedObject,
    payload: ResourcePayload,
    extension: str = ".wav",
) -> Path:
    class_dir = f"{obj.class_id:03d}_{safe_name(obj.type_name)}"
    file_name = f"{safe_name(payload.name) or obj.type_name}__path{obj.path_id}{extension}"
    return output_root / "converted" / "audio" / class_dir / file_name

def mesh_glb_output_path(output_root: Path, asset: SerializedAsset, obj: SerializedObject, name: str) -> Path:
    class_dir = f"{obj.class_id:03d}_{safe_name(obj.type_name)}"
    file_name = f"{safe_name(name) or obj.type_name}__path{obj.path_id}.glb"
    return output_root / "converted" / "mesh_glb" / class_dir / file_name

def mono_json_output_path(output_root: Path, asset: SerializedAsset, obj: SerializedObject, name: str) -> Path:
    class_dir = f"{obj.class_id:03d}_{safe_name(obj.type_name)}"
    file_name = f"{safe_name(name) or obj.type_name}__path{obj.path_id}.json"
    return output_root / "converted" / "monobehaviour_json" / class_dir / file_name

def monoscript_json_output_path(output_root: Path, obj: SerializedObject, name: str) -> Path:
    class_dir = f"{obj.class_id:03d}_{safe_name(obj.type_name)}"
    file_name = f"{safe_name(name) or obj.type_name}__path{obj.path_id}.json"
    return output_root / "converted" / "monoscript_json" / class_dir / file_name

def animation_json_output_path(output_root: Path, asset: SerializedAsset, obj: SerializedObject, name: str) -> Path:
    class_dir = f"{obj.class_id:03d}_{safe_name(obj.type_name)}"
    file_name = f"{safe_name(name) or obj.type_name}__path{obj.path_id}.json"
    return output_root / "converted" / "animation_clip_json" / class_dir / file_name

def animation_glb_output_path(output_root: Path, obj: SerializedObject, name: str) -> Path:
    class_dir = f"{obj.class_id:03d}_{safe_name(obj.type_name)}"
    file_name = f"{safe_name(name) or obj.type_name}__path{obj.path_id}.glb"
    return output_root / "converted" / "animation_glb" / class_dir / file_name

def animation_package_output_path(output_root: Path, obj: SerializedObject, name: str) -> Path:
    class_dir = f"{obj.class_id:03d}_{safe_name(obj.type_name)}"
    file_name = f"{safe_name(name) or obj.type_name}__path{obj.path_id}.sanim"
    return output_root / "converted" / "animation" / class_dir / file_name

def map_component_json_output_path(output_root: Path, obj: SerializedObject, name: str) -> Path:
    class_dir = f"{obj.class_id:03d}_{safe_name(obj.type_name)}"
    file_name = f"{safe_name(name) or obj.type_name}__path{obj.path_id}.json"
    return output_root / "converted" / "map_component_json" / class_dir / file_name

def editable_asset_json_output_path(output_root: Path, obj: SerializedObject, name: str) -> Path:
    class_dir = f"{obj.class_id:03d}_{safe_name(obj.type_name)}"
    extension = ".sqlite" if obj.class_id in TABLE_ASSET_CLASS_IDS else ".json"
    file_name = f"{safe_name(name) or obj.type_name}__path{obj.path_id}{extension}"
    return output_root / "converted" / "editable_asset" / class_dir / file_name

def material_json_output_path(output_root: Path, asset: SerializedAsset, obj: SerializedObject, name: str) -> Path:
    class_dir = f"{obj.class_id:03d}_{safe_name(obj.type_name)}"
    file_name = f"{safe_name(name) or obj.type_name}__path{obj.path_id}.json"
    return output_root / "converted" / "material_json" / class_dir / file_name

def shader_output_path(output_root: Path, asset: SerializedAsset, obj: SerializedObject, name: str) -> Path:
    class_dir = f"{obj.class_id:03d}_{safe_name(obj.type_name)}"
    file_name = f"{safe_name(name) or obj.type_name}__path{obj.path_id}.sshader"
    return output_root / "converted" / "shader" / class_dir / file_name

def cgprogram_output_path(output_root: Path, obj: SerializedObject, name: str) -> Path:
    class_dir = f"{obj.class_id:03d}_{safe_name(obj.type_name)}"
    file_name = f"{safe_name(name) or obj.type_name}__path{obj.path_id}.json"
    return output_root / "converted" / "shader" / class_dir / file_name

def textmesh_json_output_path(output_root: Path, asset: SerializedAsset, obj: SerializedObject, name: str) -> Path:
    class_dir = f"{obj.class_id:03d}_{safe_name(obj.type_name)}"
    file_name = f"{safe_name(name) or obj.type_name}__path{obj.path_id}.json"
    return output_root / "converted" / "textmesh_json" / class_dir / file_name

def ui_json_output_path(output_root: Path, obj: SerializedObject, name: str) -> Path:
    class_dir = f"{obj.class_id:03d}_{safe_name(obj.type_name)}"
    file_name = f"{safe_name(name) or obj.type_name}__path{obj.path_id}.json"
    return output_root / "converted" / "ui_json" / class_dir / file_name

def reserve_output_path(path: Path, reserved_paths: set[str], source_key: str) -> Path:
    key = path_key(path)
    if key not in reserved_paths:
        reserved_paths.add(key)
        return path

    collision_id = short_source_id(source_key)
    candidate = path.with_name(f"{path.stem}__c{collision_id}{path.suffix}")
    candidate_key = path_key(candidate)
    if candidate_key not in reserved_paths:
        reserved_paths.add(candidate_key)
        return candidate

    index = 2
    while True:
        candidate = path.with_name(f"{path.stem}__c{collision_id}_{index}{path.suffix}")
        candidate_key = path_key(candidate)
        if candidate_key not in reserved_paths:
            reserved_paths.add(candidate_key)
            return candidate
        index += 1

def path_key(path: Path) -> str:
    return str(path).replace("\\", "/").lower()

def short_source_id(source_key: str) -> str:
    return hashlib.blake2s(source_key.replace("\\", "/").lower().encode("utf-8"), digest_size=4).hexdigest()

def resolve_resource(asset: SerializedAsset, payload: ResourcePayload) -> Path | None:
    if payload.source_path == asset.relative_path:
        return asset.path
    candidate = asset.path.parent / payload.source_path
    if candidate.exists():
        return candidate
    matches = list(asset.path.parent.rglob(Path(payload.source_path).name))
    return matches[0] if matches else None

def safe_name(value: str | None, max_len: int = 120) -> str:
    if not value:
        return ""
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        return ""
    return value[:max_len].rstrip(" .")

def rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name
