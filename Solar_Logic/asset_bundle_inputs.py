from __future__ import annotations
import hashlib, json, re, struct, tempfile
from functools import lru_cache
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable
from .asset_bundle_writer import (
    ANIMATION_CLIP_CLASS_ID,
    AUDIO_CLIP_CLASS_ID,
    FONT_CLASS_ID,
    MATERIAL_CLASS_ID,
    MESH_CLASS_ID,
    PHYSIC_MATERIAL_CLASS_ID,
    SPRITE_CLASS_ID,
    TEXTURE2D_CLASS_ID,
    TEXT_ASSET_CLASS_ID,
    BundleObject,
    align_buffer,
    build_asset_bundle_bytes,
)
from .asset_bundle_animation import (
    NewAnimationClipAssetError,
    build_animation_package_asset,
    try_build_animation_clip_asset,
)
from .asset_bundle_audio import NewAudioAssetError, build_audio_asset, patch_audio_resource
from .asset_bundle_font import NewFontAssetError, build_font_asset
from .asset_bundle_shader import (
    SHADER_CLASS_ID,
    try_build_shader_asset,
)
from .asset_bundle_material import (
    BundlePointerRef,
    NewMaterialAssetError,
    try_build_material_asset,
)
from .asset_bundle_mesh import NewMeshAssetError, build_mesh_asset
from .asset_bundle_physics import (
    NewPhysicMaterialAssetError,
    try_build_physic_material_asset,
)
from .asset_bundle_cubemap import (
    CUBEMAP_CLASS_ID,
    try_build_cubemap_asset,
)
from .asset_bundle_editable import (
    NewEditableAssetError,
    build_editable_asset,
    try_read_editable_document,
)
from .asset_bundle_prefab import bundle_stub_asset, build_mono_script_object
from .asset_bundle_writer import ScriptTypeRef
from .asset_bundle_writeback import try_build_writeback_asset
from .taildata import read_taildata
from .class_ids import class_name as lookup_class_name
from .asset_bundle_plan import build_bundle_plan
from .asset_bundle_prefab import try_read_mono_document, try_read_prefab_document
from .asset_bundle_sprite import (
    NewSpriteAssetError,
    build_sprite_asset,
    try_read_sprite_document,
)
from .asset_bundle_text import build_text_asset
from .asset_bundle_texture import NewTextureAssetError, build_texture_asset
from .animation_glb_import import read_glb
from .binary import EndianReader
from .bundle_extract import read_unity_fs
from .serialized import load_serialized_asset

AUDIO_EXTENSIONS = {".wav", ".ogg"}
FONT_EXTENSIONS = {".ttf", ".otf"}
MESH_EXTENSIONS = {".glb"}
ANIMATION_EXTENSIONS = {".sanim"}
TEXTURE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tga"}
TEXT_EXTENSIONS = {
    ".txt",
    ".json",
    ".xml",
    ".csv",
    ".yaml",
    ".yml",
    ".bytes",
    ".bin",
    ".dat",
    ".cfg",
    ".ini",
}
SHADER_EXTENSIONS = {".bin"}
SHADER_PACKAGE_EXTENSIONS = {".sshader"}
TABLE_EXTENSIONS = {".sqlite"}
RAW_OBJECT_EXTENSIONS = {".uobj"}
SUPPORTED_EXTENSIONS = (
    AUDIO_EXTENSIONS
    | FONT_EXTENSIONS
    | SHADER_PACKAGE_EXTENSIONS
    | MESH_EXTENSIONS
    | ANIMATION_EXTENSIONS
    | TEXTURE_EXTENSIONS
    | TEXT_EXTENSIONS
    | SHADER_EXTENSIONS
    | TABLE_EXTENSIONS
    | RAW_OBJECT_EXTENSIONS
)
PATH_ID_PATTERN = re.compile(r"__path(-?\d+)(?:__|\.|\"|$)", re.IGNORECASE)

class AssetBundleInputError(ValueError):
    pass

@dataclass(frozen=True, slots=True)
class PreparedBundleAsset:
    input_path: Path
    name: str
    asset_path: str
    class_id: int
    class_name: str
    payload: bytes
    resource_payload: bytes | None
    resource_kind: str | None
    import_details: dict[str, object]
    pointer_refs: tuple[BundlePointerRef, ...] = ()
    script_ref: tuple[str, str, str] | None = None

@dataclass(frozen=True, slots=True)
class DeferredSpriteInput:
    input_path: Path
    document: dict[str, Any]
    asset_index: int

@dataclass(frozen=True, slots=True)
class DeferredSpritePngInput:
    input_path: Path
    asset_index: int

@dataclass(frozen=True, slots=True)
class DeferredGraphInput:
    input_path: Path
    document: dict[str, Any]
    asset_index: int
    kind: str

@dataclass(frozen=True, slots=True)
class AssetBundleBuildResult:
    output_path: Path
    manifest_path: Path
    bundle_name: str
    cab_name: str
    serialized_size: int
    resource_size: int
    bundle_size: int
    assets: tuple[dict[str, Any], ...]

@dataclass(slots=True)
class CollectedAssets:
    prepared: list[PreparedBundleAsset]
    objects: list[BundleObject]
    resources: dict[int, bytes]
    manifest_assets: list[dict[str, Any]]
    graphs: list[PreparedGraph]
    source_roots: list[Path]
    inputs: list[Path]

def generate_asset_bundle(
    output_path: Path,
    bundle_name: str,
    input_paths: Iterable[Path],
    data_root: Path | None = None,
    rebind_spec: dict[str, Any] | None = None,
) -> AssetBundleBuildResult:
    from dataclasses import replace

    clean_name = sanitize_bundle_name(bundle_name)
    collected = collect_new_assets(input_paths, data_root)
    source_roots, inputs, manifest_assets = collected.source_roots, collected.inputs, collected.manifest_assets
    cab_name = make_cab_name(clean_name, collected.prepared)
    resource_name = f"{cab_name}.resource"
    archive_source = f"archive:/{cab_name}/{resource_name}"
    resource_data = bytearray()
    bundle_objects: list[BundleObject] = []
    for item in collected.objects:
        resource = collected.resources.get(item.path_id)
        if resource is not None:
            align_buffer(resource_data, 32)
            resource_offset = len(resource_data)
            resource_data.extend(resource)
            item = replace(item, payload=patch_audio_resource(item.payload, archive_source, resource_offset, len(resource)))
        bundle_objects.append(item)

    rebind: dict[str, Any] | None = None
    if rebind_spec:
        rebind = build_embedded_rebind_map(rebind_spec, clean_name, manifest_assets, data_root)

    bundle_bytes, serialized = build_asset_bundle_bytes(
        clean_name,
        cab_name,
        bundle_objects,
        resource_name,
        bytes(resource_data),
    )
    output_path = Path(output_path).resolve()
    ensure_safe_output(output_path, inputs)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(bundle_bytes)
    manifest_path = output_path.with_suffix(output_path.suffix + ".json")
    manifest = {
        "format": "sunflowyr-rwby-assetbundle",
        "version": 1,
        "game": "rwby-ge",
        "unityVersion": "5.4.1p4",
        "serializedFormatVersion": 15,
        "targetPlatform": "Windows",
        "compression": "none",
        "authoringModel": "new-assets-only",
        "bundleName": clean_name,
        "cabName": cab_name,
        "resourceNode": resource_name if resource_data else None,
        "assetCount": len(manifest_assets),
        "assets": manifest_assets,
        "bundleSha256": hashlib.sha256(bundle_bytes).hexdigest(),
    }
    plan = build_bundle_plan(clean_name, manifest_assets)
    manifest["registrations"] = plan["registrations"]
    manifest["rebinds"] = (rebind or {}).get("entries", []) + collect_clone_rebinds(source_roots, manifest_assets)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return AssetBundleBuildResult(
        output_path=output_path,
        manifest_path=manifest_path,
        bundle_name=clean_name,
        cab_name=cab_name,
        serialized_size=len(serialized),
        resource_size=len(resource_data),
        bundle_size=len(bundle_bytes),
        assets=tuple(manifest_assets),
    )

def collect_new_assets(
    input_paths: Iterable[Path],
    data_root: Path | None = None,
    ids: Any | None = None,
) -> CollectedAssets:
    source_roots = [Path(item) for item in input_paths]
    inputs = collect_source_files(source_roots)
    if not inputs:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise AssetBundleInputError(f"stage at least one supported source asset: {supported}")

    prepared: list[PreparedBundleAsset] = []
    deferred_sprites: list[DeferredSpriteInput] = []
    deferred_sprite_pngs: list[DeferredSpritePngInput] = []
    deferred_graphs: list[DeferredGraphInput] = []
    used_paths: set[str] = set()
    for asset_index, input_path in enumerate(inputs, start=1):
        result = prepare_source_asset(input_path, asset_index, used_paths, data_root)
        if isinstance(result, DeferredSpriteInput):
            deferred_sprites.append(result)
        elif isinstance(result, DeferredSpritePngInput):
            deferred_sprite_pngs.append(result)
        elif isinstance(result, DeferredGraphInput):
            deferred_graphs.append(result)
        else:
            prepared.append(result)

    prepared.extend(prepare_sprite_png_sources(deferred_sprite_pngs, used_paths, data_root))
    for deferred in deferred_sprites:
        prepared.append(prepare_sprite_source(deferred, prepared, used_paths))

    prepared = resolve_pointer_refs(prepared, ids)
    graphs, script_objects = build_graph_inputs(deferred_graphs, used_paths, data_root)
    resources: dict[int, bytes] = {}
    bundle_objects: list[BundleObject] = []
    manifest_assets: list[dict[str, Any]] = []
    script_ids: dict[tuple[str, str, str], int] = {}
    next_script_id = len(prepared) + 2
    for item in prepared:
        if item.script_ref is not None and item.script_ref not in script_ids:
            if ids is not None:
                raise AssetBundleInputError(f"{item.input_path.name}: an edited game MonoBehaviour replaces its original and cant become a new object")
            script_ids[item.script_ref] = next_script_id
            next_script_id += 1

    for index, item in enumerate(prepared, start=2):
        object_payload = item.payload
        if ids is not None:
            index = ids.asset(item)
        if item.resource_kind == "audio" and item.resource_payload is not None:
            resources[index] = item.resource_payload
        script = None
        if item.script_ref is not None:
            script_path_id = script_ids[item.script_ref]
            object_payload = point_at_bundle_script(object_payload, script_path_id)
            script = ScriptTypeRef(
                assembly=item.script_ref[0],
                namespace=item.script_ref[1],
                class_name=item.script_ref[2],
                path_id=script_path_id,
            )
        bundle_objects.append(
            BundleObject(
                path_id=index,
                class_id=item.class_id,
                asset_path=item.asset_path,
                name=item.name,
                payload=object_payload,
                script=script,
            )
        )
        manifest_assets.append(
            {
                "assetPath": item.asset_path,
                "name": item.name,
                "sourceFile": item.input_path.name,
                "classId": item.class_id,
                "className": item.class_name,
                "bundlePathId": index,
                "import": item.import_details,
            }
        )

    for key, script_path_id in sorted(script_ids.items(), key=lambda entry: entry[1]):
        reference = ScriptTypeRef(assembly=key[0], namespace=key[1], class_name=key[2],
                                  path_id=script_path_id)
        built = build_mono_script_object(reference)
        bundle_objects.append(
            BundleObject(
                path_id=script_path_id,
                class_id=built.class_id,
                asset_path="",
                name=built.name,
                payload=built.payload,
                container_listed=False,
            )
        )

    if graphs or script_objects:
        append_graph_objects(
            graphs,
            script_objects,
            prepared,
            bundle_objects,
            manifest_assets,
            ids,
        )

    verify_unique_asset_paths(bundle_objects)
    return CollectedAssets(prepared, bundle_objects, resources, manifest_assets, graphs, source_roots, inputs)

@dataclass(slots=True)
class PreparedGraph:
    input_path: Path
    asset_path: str
    name: str
    kind: str
    objects: list[Any]
    details: dict[str, Any]
    external_refs: dict[int, str]

def build_graph_inputs(
    deferred: list[DeferredGraphInput],
    used_paths: set[str],
    data_root: Path | None,
) -> tuple[list[PreparedGraph], list[Any]]:
    if not deferred:
        return [], []
    from .asset_bundle_prefab import (
        NewPrefabAssetError,
        SentinelAllocator,
        build_mono_asset,
        build_mono_script_object,
        build_prefab_objects,
    )
    from .asset_bundle_writer import ScriptTypeRef
    from .asset_bundle_prefab import plan_prefab_nodes

    registry = load_managed_registry(data_root)
    sentinels = SentinelAllocator()
    script_pool: dict[tuple[str, str, str], int] = {}
    graphs: list[PreparedGraph] = []

    bundle_plans: dict[str, Any] = {}
    for item in deferred:
        if item.kind != "prefab":
            continue
        try:
            bundle_plans[item.input_path.name.casefold()] = plan_prefab_nodes(
                item.document, sentinels
            )
        except NewPrefabAssetError as exc:
            raise AssetBundleInputError(f"{item.input_path.name}: {exc}") from exc

    for item in deferred:
        name = source_display_name(item.input_path)
        try:
            if item.kind == "prefab":
                build = build_prefab_objects(
                    item.document,
                    name,
                    sentinels,
                    script_pool,
                    registry,
                    plan=bundle_plans.get(item.input_path.name.casefold()),
                    bundle_plans=bundle_plans,
                )
                category, suffix = "prefabs", ".prefab"
            elif item.kind == "editable":
                build = build_editable_asset(
                    item.document,
                    name,
                    sentinels,
                    bundle_plans=bundle_plans,
                )
                category, suffix = "assets", ".asset"
            else:
                build = build_mono_asset(
                    item.document,
                    name,
                    sentinels,
                    script_pool,
                    registry,
                    bundle_plans=bundle_plans,
                )
                category, suffix = "definitions", ".asset"
        except (NewPrefabAssetError, NewEditableAssetError) as exc:
            raise AssetBundleInputError(f"{item.input_path.name}: {exc}") from exc
        asset_path = make_asset_path(
            category,
            build.root_name,
            suffix,
            f"new-{item.asset_index}",
            used_paths,
        )
        graphs.append(
            PreparedGraph(
                input_path=item.input_path,
                asset_path=asset_path,
                name=build.root_name,
                kind=item.kind,
                objects=build.objects,
                details=build.details,
                external_refs=build.external_refs,
            )
        )
    script_objects = [
        build_mono_script_object(
            ScriptTypeRef(assembly=key[0], namespace=key[1], class_name=key[2], path_id=path_id)
        )
        for key, path_id in script_pool.items()
    ]
    return graphs, script_objects

def build_embedded_rebind_map(
    spec: dict[str, Any],
    bundle_name: str,
    manifest_assets: list[dict[str, Any]],
    data_root: Path | None,
) -> dict[str, Any] | None:
    from .model_context import build_model_context
    from .replacement_map import (
        build_replacement_map,
        resolve_targets_by_name,
        resolve_targets_from_taildata,
    )
    from .serialized import discover_serialized_files, load_serialized_asset

    replace_paths = [str(item) for item in (spec.get("replace") or [])]
    taildata_paths = [str(item) for item in (spec.get("taildata") or [])]
    targets_file = spec.get("targetsFile")
    name_requests: list[dict[str, Any]] = []
    if targets_file:
        try:
            document = json.loads(Path(targets_file).read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AssetBundleInputError(f"couldnt read rebind targets file: {exc}") from exc
        raw = document.get("targets") if isinstance(document, dict) else document
        if not isinstance(raw, list):
            raise AssetBundleInputError('rebind targets file needs a "targets" list')
        name_requests = [item for item in raw if isinstance(item, dict)]
    if not replace_paths and not name_requests:
        return None

    root = Path(data_root).resolve() if data_root else Path.cwd() / "rwby-ge_Data"
    if not root.is_dir():
        raise AssetBundleInputError(
            f"rebind map needs the game data folder, {root} doesnt exist (pass --data)"
        )
    serialized = [load_serialized_asset(path, root) for path in discover_serialized_files(root)]
    context = build_model_context(serialized)

    targets = []
    problems: list[str] = []
    if replace_paths:
        staged_targets, staged_problems = resolve_targets_from_taildata(
            replace_paths, taildata_paths, manifest_assets
        )
        targets.extend(staged_targets)
        problems.extend(staged_problems)
    if name_requests:
        named_targets, named_problems = resolve_targets_by_name(
            context, name_requests, manifest_assets
        )
        targets.extend(named_targets)
        problems.extend(named_problems)
    if not targets:
        raise AssetBundleInputError(
            "no rebind targets resolved: " + ("; ".join(problems) or "nothing matched")
        )

    mono_scripts = None
    managed_registry = None
    if spec.get("scanMono"):
        from .managed_schema import shared_registry
        from .mono_export import build_monoscript_index

        mono_scripts = build_monoscript_index(serialized)
        managed_registry = shared_registry(root / "Managed")

    document, stats = build_replacement_map(
        context,
        targets,
        bundle_name=bundle_name,
        scan_mono=bool(spec.get("scanMono")),
        mono_scripts=mono_scripts,
        managed_registry=managed_registry,
    )
    if problems:
        document["targetProblems"] = problems
    return document

def load_managed_registry(data_root: Path | None) -> Any | None:
    candidates = []
    if data_root is not None:
        candidates.append(Path(data_root))
    candidates.append(Path.cwd() / "rwby-ge_Data")
    candidates.append(Path(__file__).resolve().parent.parent / "rwby-ge_Data")
    for candidate in candidates:
        managed = candidate / "Managed"
        if managed.is_dir():
            try:
                from .managed_schema import shared_registry

                return shared_registry(managed)
            except Exception:
                return None
    return None

def point_at_bundle_script(payload: bytes, script_path_id: int) -> bytes:
    from .mono_repack import mono_script_pointer_offset

    offset = mono_script_pointer_offset(bundle_stub_asset())
    if offset + 12 > len(payload):
        raise AssetBundleInputError(
            "rebuilt MonoBehaviour is too short to hold an m_Script pointer"
        )
    out = bytearray(payload)
    struct.pack_into("<i", out, offset, 0)
    struct.pack_into("<q", out, offset + 4, int(script_path_id))
    return bytes(out)

def append_graph_objects(
    graphs: list[PreparedGraph],
    script_objects: list[Any],
    prepared: list[PreparedBundleAsset],
    bundle_objects: list[BundleObject],
    manifest_assets: list[dict[str, Any]],
    ids: Any | None = None,
) -> None:
    from .asset_bundle_prefab import substitute_sentinels
    from .asset_bundle_writer import ScriptTypeRef

    next_id = len(bundle_objects) + 2
    sentinel_map: dict[int, int] = {}
    if ids is None:
        for built in script_objects:
            sentinel_map[built.local_id] = next_id
            next_id += 1
    else:
        for graph in graphs:
            for built in graph.objects:
                if built.script is not None:
                    sentinel_map[built.script.path_id] = ids.script(built.script.key, graph.input_path)
    graph_spans: list[tuple[PreparedGraph, int, int]] = []
    for graph in graphs:
        start_id = next_id if ids is None else ids.graph(graph)
        for offset, built in enumerate(graph.objects):
            sentinel_map[built.local_id] = start_id + offset
        if ids is None:
            next_id += len(graph.objects)
        graph_spans.append((graph, start_id, len(graph.objects)))
    by_source = {item.input_path.name.casefold(): (ids.asset(item) if ids is not None else index + 2) - 2 for index, item in enumerate(prepared)}
    graph_by_source = {
        graph.input_path.name.casefold(): start for graph, start, _ in graph_spans
    }
    for graph in graphs:
        for sentinel, source_name in graph.external_refs.items():
            wanted = Path(str(source_name).replace("\\", "/")).name.casefold()
            if wanted in by_source:
                sentinel_map[sentinel] = by_source[wanted] + 2
            elif wanted in graph_by_source:
                sentinel_map[sentinel] = graph_by_source[wanted]
            else:
                raise AssetBundleInputError(
                    f'{graph.input_path.name}: references "{source_name}" '
                    "but that file isnt staged in this bundle"
                )

    for built in script_objects if ids is None else []:
        bundle_objects.append(
            BundleObject(
                path_id=sentinel_map[built.local_id],
                class_id=built.class_id,
                asset_path="",
                name=built.name,
                payload=built.payload,
                container_listed=False,
            )
        )

    for graph, start_id, span in graph_spans:
        for offset, built in enumerate(graph.objects):
            path_id = start_id + offset
            script = None
            if built.script is not None:
                script = ScriptTypeRef(
                    assembly=built.script.assembly,
                    namespace=built.script.namespace,
                    class_name=built.script.class_name,
                    path_id=sentinel_map[built.script.path_id],
                )
            bundle_objects.append(
                BundleObject(
                    path_id=path_id,
                    class_id=built.class_id,
                    asset_path=graph.asset_path if built.container_listed else "",
                    name=built.name,
                    payload=substitute_sentinels(built.payload, sentinel_map),
                    script=script,
                    container_listed=built.container_listed,
                    preload_span=span if built.container_listed else 1,
                )
            )
        main = graph.objects[0]
        if not main.container_listed:
            continue
        manifest_assets.append(
            {
                "assetPath": graph.asset_path,
                "name": graph.name,
                "sourceFile": graph.input_path.name,
                "classId": main.class_id,
                "className": main.class_name,
                "bundlePathId": start_id,
                "import": graph.details,
            }
        )

def collect_source_files(input_paths: Iterable[Path]) -> list[Path]:
    outputs: dict[Path, Path] = {}
    for value in input_paths:
        path = Path(value).resolve()
        if path.is_dir():
            candidates = path.rglob("*")
        else:
            if path.is_file() and path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                raise AssetBundleInputError(f"unsupported source asset extension: {path.name}")
            candidates = [path]
        for candidate in candidates:
            if candidate.is_file() and candidate.suffix.lower() in SUPPORTED_EXTENSIONS:
                if is_generated_sidecar(candidate):
                    continue
                outputs[candidate.resolve()] = candidate.resolve()
    animation_overrides = {
        item.with_suffix(".glb").resolve()
        for item in outputs.values()
        if item.suffix.lower() == ".sanim"
    }
    for override in animation_overrides:
        outputs.pop(override, None)
    for item in list(outputs.values()):
        if item.suffix.lower() != ".wav":
            continue
        sibling = item.with_suffix(".ogg").resolve()
        record = read_taildata(item) if sibling in outputs else None
        if isinstance(record, dict) and str(record.get("kind") or "").startswith("audio_"):
            older = sibling if item.stat().st_mtime >= sibling.stat().st_mtime else item
            outputs.pop(older, None)
    return drop_identical_copies(sorted(outputs.values(), key=lambda item: str(item).casefold()))

def drop_identical_copies(paths: list[Path]) -> list[Path]:
    by_name: dict[str, list[Path]] = {}
    for item in paths:
        by_name.setdefault(item.name.casefold(), []).append(item)
    dropped: set[Path] = set()
    for group in by_name.values():
        if len(group) < 2:
            continue
        seen: set[str] = set()
        for item in group:
            digest = file_digest(item)
            if digest in seen:
                dropped.add(item)
            else:
                seen.add(digest)
    return [item for item in paths if item not in dropped]

def file_digest(path: Path) -> str:
    stat = Path(path).stat()
    return cached_digest(str(Path(path).resolve()), stat.st_size, stat.st_mtime_ns)

@lru_cache(maxsize=4096)
def cached_digest(path_text: str, size: int, stamp: int) -> str:
    return hashlib.sha256(Path(path_text).read_bytes()).hexdigest()

def collect_clone_rebinds(
    input_paths: Iterable[Path], manifest_assets: list[dict[str, Any]] | None = None
) -> list[dict]:
    entries: list[dict] = []
    seen: set[str] = set()
    for value in input_paths:
        path = Path(value)
        candidates = [path / "sunflowyr_rebinds.json"] if path.is_dir() else []
        if path.is_file() and path.name.casefold() == "sunflowyr_rebinds.json":
            candidates = [path]
        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                document = json.loads(candidate.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            for entry in document.get("rebinds") or []:
                if not isinstance(entry, dict):
                    continue
                key = json.dumps(entry, sort_keys=True)
                if key not in seen:
                    seen.add(key)
                    entries.append(entry)
    by_source = {
        str(item.get("sourceFile") or ""): item.get("assetPath")
        for item in (manifest_assets or [])
    }
    for entry in entries:
        target = by_source.get(str(entry.get("asset") or ""))
        if target:
            entry["assetPath"] = target
    return entries

def is_sprite_png(path: Path) -> bool:
    if path.suffix.lower() != ".png":
        return False
    record = read_taildata(path)
    return isinstance(record, dict) and record.get("kind") == "sprite_png"

def prepare_sprite_png_sources(
    deferred: list[DeferredSpritePngInput],
    used_paths: set[str],
    data_root: Path | None,
) -> list[PreparedBundleAsset]:
    if not deferred:
        return []
    if data_root is None:
        raise AssetBundleInputError(
            "sprite PNG writeback needs the game data folder to find the atlas texture"
        )
    from .sprite_repack import (
        SpriteRepackError,
        prepare_sprite_png,
        repack_sprite_png_group,
    )

    groups: dict[tuple[str, int], list] = {}
    order: dict[tuple[str, int], DeferredSpritePngInput] = {}
    for item in deferred:
        record = read_taildata(item.input_path)
        if not isinstance(record, dict):
            raise AssetBundleInputError(
                f"{item.input_path.name}: sprite PNG has no taildata record naming its sprite"
            )
        try:
            edit = prepare_sprite_png(item.input_path, record, Path(data_root))
        except SpriteRepackError as exc:
            raise AssetBundleInputError(f"{item.input_path.name}: {exc}") from exc
        groups.setdefault(edit.target_key, []).append(edit)
        order.setdefault(edit.target_key, item)

    prepared: list[PreparedBundleAsset] = []
    for key, edits in groups.items():
        try:
            payload, details = repack_sprite_png_group(edits)
        except SpriteRepackError as exc:
            raise AssetBundleInputError(
                f"{order[key].input_path.name}: sprite PNG writeback failed: {exc}"
            ) from exc
        name = str(details.get("name") or edits[0].texture_object.path_id)
        details = dict(details)
        details["sourceFormat"] = "sunflowyr-unpack-writeback"
        details["kind"] = "sprite_png"
        details["spriteCount"] = len(edits)
        details["sprites"] = [edit.sprite_name for edit in edits[:12]]
        prepared.append(
            PreparedBundleAsset(
                input_path=order[key].input_path,
                name=name,
                asset_path=make_asset_path(
                    "writeback", name, ".asset", f"new-{order[key].asset_index}", used_paths
                ),
                class_id=TEXTURE2D_CLASS_ID,
                class_name="Texture2D",
                payload=payload,
                resource_payload=None,
                resource_kind=None,
                import_details=details,
                pointer_refs=(),
            )
        )
    return prepared

def is_terrain_glb(path: Path) -> bool:
    if path.suffix.lower() != ".glb":
        return False
    record = read_taildata(path)
    return isinstance(record, dict) and record.get("kind") == "reconstructed_terrain_glb"

def is_generated_sidecar(path: Path) -> bool:
    lowered = path.name.casefold()
    return (
        lowered.endswith(".shader.deps.json")
        or lowered == "sunflowyr_rebinds.json"
        or lowered == "sunflowyr_clone_report.log"
        or lowered.endswith(".build.log")
        or lowered.endswith(".clone-report.json")
        or lowered.endswith(".bundle.json")
        or lowered.endswith(".bundle.plan.json")
        or lowered.endswith(".settings.json")
        or lowered.endswith("_collision.bin")
        or lowered.endswith(".suntex")
        or lowered.endswith(".tiles.bin")
        or lowered.endswith(".u8.bin")
        or lowered.endswith(".u16le.bin")
        or lowered.endswith(".f32le.bin")
        or lowered.endswith(".pvs.bin")
        or lowered == "sunflowyr_taildata.json"
        or has_binary_role(path)
    )

def has_binary_role(path: Path) -> bool:
    record = read_taildata(path)
    if not isinstance(record, dict):
        return False
    if record.get("binaryRole") or record.get("binary_role"):
        return True
    return str(record.get("kind") or "").endswith("_binary")

def prepare_source_asset(
    input_path: Path,
    asset_index: int,
    used_paths: set[str],
    data_root: Path | None = None,
) -> PreparedBundleAsset | DeferredSpriteInput | DeferredSpritePngInput | DeferredGraphInput:
    suffix = input_path.suffix.lower()
    name = source_display_name(input_path)
    asset_suffix = suffix
    identity = f"new-{asset_index}"
    pointer_refs: tuple[BundlePointerRef, ...] = ()
    script_ref: tuple[str, str, str] | None = None
    try:
        if is_sprite_png(input_path):
            return DeferredSpritePngInput(input_path=input_path, asset_index=asset_index)
        if (writeback := try_build_writeback_asset(input_path, data_root)) is not None:
            (payload, name, class_id, details, resource_payload,
             resource_kind, script_ref) = writeback
            class_name = lookup_class_name(class_id)
            category = "writeback"
            asset_suffix = ".asset"
        elif suffix in AUDIO_EXTENSIONS:
            payload, resource_payload, details = build_audio_asset(input_path, name)
            class_id = AUDIO_CLIP_CLASS_ID
            class_name = "AudioClip"
            category = "audio"
            resource_kind = "audio"
        elif suffix in FONT_EXTENSIONS:
            payload, details = build_font_asset(input_path, name)
            resource_payload = None
            class_id = FONT_CLASS_ID
            class_name = "Font"
            category = "fonts"
            resource_kind = None
        elif suffix in MESH_EXTENSIONS and not is_terrain_glb(input_path):
            from .navmesh_from_glb import looks_like_navmesh_glb
            from .prefab_from_glb import is_hierarchy_glb

            if looks_like_navmesh_glb(input_path):
                raise AssetBundleInputError(
                    f'"{input_path.name}" is a navmesh GLB, convert it first with: '
                    f'solar navmesh-from-glb "{input_path.name}" --out <folder>'
                )
            try:
                hierarchy_document, _ = read_glb(input_path.read_bytes())
            except Exception:
                hierarchy_document = None
            if hierarchy_document is not None and is_hierarchy_glb(hierarchy_document):
                raise AssetBundleInputError(
                    f'"{input_path.name}" is a scene or prefab GLB, convert it first with: '
                    f'solar prefab-from-glb "{input_path.name}" --out <folder>'
                )
            payload, name, details = build_mesh_asset(input_path, name)
            resource_payload = None
            class_id = MESH_CLASS_ID
            class_name = "Mesh"
            category = "meshes"
            resource_kind = None
            asset_suffix = ".mesh"
        elif suffix in TEXTURE_EXTENSIONS:
            payload, details = build_texture_asset(input_path, name)
            resource_payload = None
            class_id = TEXTURE2D_CLASS_ID
            class_name = "Texture2D"
            category = "textures"
            resource_kind = None
        elif suffix in ANIMATION_EXTENSIONS:
            payload, name, details, pointer_refs = build_animation_package_asset(input_path, name)
            resource_payload = None
            class_id = ANIMATION_CLIP_CLASS_ID
            class_name = "AnimationClip"
            category = "animations"
            resource_kind = None
            asset_suffix = ".anim"
        elif suffix == ".json" and (
            animation_clip := try_build_animation_clip_asset(input_path, name)
        ) is not None:
            payload, name, details, pointer_refs = animation_clip
            resource_payload = None
            class_id = ANIMATION_CLIP_CLASS_ID
            class_name = "AnimationClip"
            category = "animations"
            resource_kind = None
            asset_suffix = ".anim"
        elif suffix == ".json" and (
            physic_material := try_build_physic_material_asset(input_path, name)
        ) is not None:
            payload, name, details = physic_material
            resource_payload = None
            class_id = PHYSIC_MATERIAL_CLASS_ID
            class_name = "PhysicMaterial"
            category = "physics"
            resource_kind = None
            asset_suffix = ".physicmaterial"
        elif (cubemap := try_build_cubemap_asset(input_path, name)) is not None:
            payload, name, details = cubemap
            resource_payload = None
            class_id = CUBEMAP_CLASS_ID
            class_name = "Cubemap"
            category = "textures"
            resource_kind = None
            asset_suffix = ".cubemap"
        elif (shader := try_build_shader_asset(input_path, name)) is not None:
            payload, name, details, pointer_refs = shader
            resource_payload = None
            class_id = SHADER_CLASS_ID
            class_name = "Shader"
            category = "shaders"
            resource_kind = None
            asset_suffix = ".shader"
        elif suffix == ".json" and (
            material := try_build_material_asset(input_path, name)
        ) is not None:
            payload, name, details, pointer_refs = material
            resource_payload = None
            class_id = MATERIAL_CLASS_ID
            class_name = "Material"
            category = "materials"
            resource_kind = None
            asset_suffix = ".mat"
        elif suffix == ".json" and (
            sprite_document := try_read_sprite_document(input_path)
        ) is not None:
            return DeferredSpriteInput(
                input_path=input_path,
                document=sprite_document,
                asset_index=asset_index,
            )
        elif suffix == ".json" and (
            editable_document := try_read_editable_document(input_path)
        ) is not None:
            return DeferredGraphInput(input_path, editable_document, asset_index, "editable")
        elif suffix == ".json" and (
            prefab_document := try_read_prefab_document(input_path)
        ) is not None:
            return DeferredGraphInput(input_path, prefab_document, asset_index, "prefab")
        elif suffix == ".json" and (
            mono_document := try_read_mono_document(input_path)
        ) is not None:
            return DeferredGraphInput(input_path, mono_document, asset_index, "mono")
        elif suffix in TEXT_EXTENSIONS:
            payload, details = build_text_asset(input_path, name)
            resource_payload = None
            class_id = TEXT_ASSET_CLASS_ID
            class_name = "TextAsset"
            category = "text"
            resource_kind = None
        elif suffix in RAW_OBJECT_EXTENSIONS:
            raise AssetBundleInputError(
                f'"{input_path.name}" is a raw object export, stage it with the sunflowyr_taildata.json next to it'
            )
        elif suffix in TABLE_EXTENSIONS:
            raise AssetBundleInputError(
                f'"{input_path.name}" is an unpacked asset table, stage it with the sunflowyr_taildata.json next to it'
            )
        else:
            raise AssetBundleInputError(f"unsupported source asset extension: {input_path.name}")
    except (
        NewAnimationClipAssetError,
        NewAudioAssetError,
        NewFontAssetError,
        NewMaterialAssetError,
        NewMeshAssetError,
        NewPhysicMaterialAssetError,
        NewSpriteAssetError,
        NewTextureAssetError,
        OSError,
    ) as exc:
        raise AssetBundleInputError(f"{input_path.name}: {exc}") from exc
    asset_path = make_asset_path(
        category,
        name,
        asset_suffix,
        identity,
        used_paths,
    )
    return PreparedBundleAsset(
        input_path=input_path,
        name=name,
        asset_path=asset_path,
        class_id=class_id,
        class_name=class_name,
        payload=payload,
        resource_payload=resource_payload,
        resource_kind=resource_kind,
        import_details=details,
        pointer_refs=pointer_refs,
        script_ref=script_ref,
    )

def prepare_sprite_source(
    deferred: DeferredSpriteInput,
    prepared: list[PreparedBundleAsset],
    used_paths: set[str],
) -> PreparedBundleAsset:
    input_path = deferred.input_path
    name = source_display_name(input_path)
    try:
        texture_file = str(deferred.document.get("texture") or "")
        if not texture_file:
            raise NewSpriteAssetError('Sprite JSON needs "texture" naming a staged image input')
        texture = find_prepared_source(prepared, texture_file, "texture", deferred.input_path)
        if texture.class_id != TEXTURE2D_CLASS_ID:
            raise NewSpriteAssetError(
                f'"{texture_file}" is a {texture.class_name}, Sprite texture must be a staged image'
            )
        payload, name, details, pointer_refs = build_sprite_asset(
            deferred.document,
            input_path,
            name,
            int(texture.import_details.get("width", 0)),
            int(texture.import_details.get("height", 0)),
        )
    except NewSpriteAssetError as exc:
        raise AssetBundleInputError(f"{input_path.name}: {exc}") from exc
    asset_path = make_asset_path(
        "sprites",
        name,
        ".sprite",
        f"new-{deferred.asset_index}",
        used_paths,
    )
    return PreparedBundleAsset(
        input_path=input_path,
        name=name,
        asset_path=asset_path,
        class_id=SPRITE_CLASS_ID,
        class_name="Sprite",
        payload=payload,
        resource_payload=None,
        resource_kind=None,
        import_details=details,
        pointer_refs=pointer_refs,
    )

def find_prepared_source(
    prepared: list[PreparedBundleAsset],
    source_name: str,
    slot: str,
    near: Path | None = None,
) -> PreparedBundleAsset:
    wanted = Path(source_name.replace("\\", "/")).name.casefold()
    matches = [item for item in prepared if item.input_path.name.casefold() == wanted]
    if not matches:
        raise NewSpriteAssetError(
            f'{slot} references "{source_name}" but that file isnt staged in this bundle'
        )
    if len(matches) > 1 and near is not None:
        local = near.parent / Path(source_name.replace("\\", "/")).name
        if local.is_file():
            digest = file_digest(local)
            same = [item for item in matches if file_digest(item.input_path) == digest]
            if same:
                return same[0]
    if len(matches) > 1:
        raise NewSpriteAssetError(
            f'{slot} reference "{source_name}" matches {len(matches)} staged files; '
            "give the inputs unique file names"
        )
    return matches[0]

def resolve_pointer_refs(prepared: list[PreparedBundleAsset], ids: Any | None = None) -> list[PreparedBundleAsset]:
    path_ids: dict[int, int] = {
        id(item): (ids.asset(item) if ids is not None else path_id) for path_id, item in enumerate(prepared, start=2)
    }
    resolved: list[PreparedBundleAsset] = []
    for item in prepared:
        if not item.pointer_refs:
            resolved.append(item)
            continue
        payload = bytearray(item.payload)
        references: list[dict[str, Any]] = []
        for ref in item.pointer_refs:
            try:
                target = find_prepared_source(prepared, ref.source_name, ref.slot, item.input_path)
            except NewSpriteAssetError as exc:
                raise AssetBundleInputError(f"{item.input_path.name}: {exc}") from exc
            if ref.slot.startswith("texture") and target.class_id not in (
                TEXTURE2D_CLASS_ID,
                CUBEMAP_CLASS_ID,
            ):
                raise AssetBundleInputError(
                    f'{item.input_path.name}: {ref.slot} references "{ref.source_name}" '
                    f"which is a {target.class_name}, not a texture"
                )
            if ref.payload_offset < 0 or ref.payload_offset + 12 > len(payload):
                raise AssetBundleInputError(
                    f"{item.input_path.name}: {ref.slot} pointer is outside the object payload"
                )
            struct.pack_into("<iq", payload, ref.payload_offset, 0, path_ids[id(target)])
            references.append(
                {
                    "slot": ref.slot,
                    "file": ref.source_name,
                    "assetPath": target.asset_path,
                    "bundlePathId": path_ids[id(target)],
                }
            )
        details = dict(item.import_details)
        details["references"] = references
        resolved.append(
            PreparedBundleAsset(
                input_path=item.input_path,
                name=item.name,
                asset_path=item.asset_path,
                class_id=item.class_id,
                class_name=item.class_name,
                payload=bytes(payload),
                resource_payload=item.resource_payload,
                resource_kind=item.resource_kind,
                import_details=details,
                pointer_refs=item.pointer_refs,
            )
        )
    return resolved

def inspect_asset_bundle(bundle_path: Path) -> dict[str, Any]:
    path = Path(bundle_path).resolve()
    nodes, block_data = read_unity_fs(path.read_bytes())
    node_details = [
        {"path": node.path, "offset": node.offset, "size": node.size, "flags": node.flags}
        for node in nodes
    ]
    serialized_nodes = [node for node in nodes if node.flags & 4]
    if len(serialized_nodes) != 1:
        raise AssetBundleInputError("generated bundle should contain one serialized node")
    serialized_node = serialized_nodes[0]
    serialized_data = block_data[
        serialized_node.offset : serialized_node.offset + serialized_node.size
    ]
    with tempfile.TemporaryDirectory(prefix="sunflowyr_bundle_") as folder:
        asset_path = Path(folder) / serialized_node.path
        asset_path.write_bytes(serialized_data)
        asset = load_serialized_asset(asset_path, Path(folder))
        class_counts = {str(key): value for key, value in sorted(asset.class_counts().items())}
        bundle_object = next((item for item in asset.objects if item.class_id == 142), None)
        if bundle_object is None:
            raise AssetBundleInputError("serialized node has no AssetBundle object")
        raw = serialized_data[
            bundle_object.byte_start : bundle_object.byte_start + bundle_object.byte_size
        ]
        asset_names = read_asset_bundle_names(raw, asset.endian)
    return {
        "path": str(path),
        "bundleSize": path.stat().st_size,
        "nodes": node_details,
        "unityVersion": asset.unity_version,
        "serializedFormatVersion": asset.format_version,
        "platform": asset.platform,
        "typeTree": asset.enable_type_tree,
        "objectCount": len(asset.objects),
        "classCounts": class_counts,
        "assetNames": asset_names,
    }

def read_asset_bundle_names(data: bytes, endian: str) -> list[str]:
    reader = EndianReader(BytesIO(data), endian)
    reader.aligned_string(max_length=4096)
    preload_count = reader.i32()
    for index in range(preload_count):
        reader.i32()
        reader.i64()
    container_count = reader.i32()
    names: list[str] = []
    for index in range(container_count):
        names.append(reader.aligned_string(max_length=16384))
        reader.i32()
        reader.i32()
        reader.i32()
        reader.i64()
    return names

def make_cab_name(bundle_name: str, assets: list[PreparedBundleAsset]) -> str:
    digest = hashlib.md5()
    digest.update(bundle_name.encode("utf-8"))
    for item in assets:
        digest.update(item.asset_path.encode("utf-8"))
        digest.update(hashlib.sha256(item.payload).digest())
        if item.resource_payload is not None:
            digest.update(hashlib.sha256(item.resource_payload).digest())
    return "CAB-" + digest.hexdigest()

def make_asset_path(
    category: str,
    name: str,
    suffix: str,
    identity: str,
    used_paths: set[str],
) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9 .()'\-]+", "-", name).strip(" .-") or "asset"
    extension = suffix.lower()
    candidate = f"assets/sunflowyr/{category}/{safe_name}{extension}".lower()
    if candidate.casefold() in used_paths:
        safe_identity = re.sub(r"[^A-Za-z0-9-]+", "-", identity).strip("-") or "copy"
        candidate = f"assets/sunflowyr/{category}/{safe_name}-{safe_identity}{extension}".lower()
        counter = 2
        while candidate.casefold() in used_paths:
            candidate = (
                f"assets/sunflowyr/{category}/{safe_name}-{safe_identity}-{counter}{extension}"
            ).lower()
            counter += 1
    used_paths.add(candidate.casefold())
    return candidate

def verify_unique_asset_paths(bundle_objects: list[BundleObject]) -> None:
    seen: dict[str, BundleObject] = {}
    clashes: list[str] = []
    for item in bundle_objects:
        if not item.container_listed or not item.asset_path:
            continue
        key = item.asset_path.casefold()
        first = seen.get(key)
        if first is None:
            seen[key] = item
            continue
        clashes.append(
            f'"{item.asset_path}" is claimed by both {lookup_class_name(first.class_id)} '
            f'"{first.name}" (path id {first.path_id}) and {lookup_class_name(item.class_id)} '
            f'"{item.name}" (path id {item.path_id})'
        )
    if clashes:
        joiner = chr(10) + "  "
        raise AssetBundleInputError(
            "duplicate asset paths in the bundle container:" + joiner + joiner.join(clashes[:10])
        )

def sanitize_bundle_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9 .()\-]+", "-", str(value).strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-")
    if not cleaned:
        raise AssetBundleInputError("bundle name cant be empty")
    return cleaned

def source_display_name(path: Path) -> str:
    return PATH_ID_PATTERN.sub("", path.stem).strip(" _-") or "Asset"

def ensure_safe_output(
    output_path: Path,
    inputs: list[Path],
) -> None:
    if output_path in {path.resolve() for path in inputs}:
        raise AssetBundleInputError("AssetBundle output cant overwrite an input asset")
    if output_path.suffix.lower() in SUPPORTED_EXTENSIONS:
        raise AssetBundleInputError("AssetBundle output mustnt use a source asset extension")
