from __future__ import annotations
import argparse, json
from pathlib import Path
from . import __version__
from .asset_bundle_inputs import AssetBundleInputError, generate_asset_bundle, inspect_asset_bundle
from .asset_bundle_writer import AssetBundleWriteError
from .bundle_extract import extract_bundle
from .class_ids import CLASS_ID_NAMES
from .exporter import ExportOptions, inventory, unpack
from .taildata import read_taildata

EDITABLE_EXPORT_CHOICES = (
    "text-assets",
    "png",
    "sprites",
    "audio",
    "model-glb",
    "reconstructed-glb",
    "mono-json",
    "animation",
    "material-json",
    "fonts",
    "shaders",
    "textmesh-json",
    "ui-json",
    "map-json",
    "asset-data",
    "gameobject-json",
)

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    return args.func(args)

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="solar", description="Sunflowyr Engine RWBY GE Modding Software")
    parser.add_argument("--version", action="version", version=f"Sunflowyr Engine {__version__}")
    sub = parser.add_subparsers(dest="command")

    inv = sub.add_parser("inventory", help="scan RWBY Unity serialized files")
    inv.add_argument("--data", type=Path, default=default_data_root(), help="rwby-ge_Data folder")
    inv.add_argument("--json", action="store_true", help="print full JSON inventory")
    inv.set_defaults(func=cmd_inventory)

    up = sub.add_parser("unpack", help="unpack native Unity objects and editable views")
    up.add_argument("--data", type=Path, default=default_data_root(), help="rwby-ge_Data folder")
    up.add_argument("--out", type=Path, default=Path("Solar_Output"), help="output folder")
    up.add_argument(
        "--raw",
        action="store_true",
        help="export every object as a complete native Unity .uobj file",
    )
    up.add_argument(
        "--full",
        action="store_true",
        help="export Raw native objects and every editable format",
    )
    up.add_argument(
        "--editable",
        action="append",
        choices=EDITABLE_EXPORT_CHOICES + ("animation-glb", "asset-json"),
        help="export only this editable family, repeatable and combinable with --raw",
    )
    up.add_argument("--no-raw", action="store_true", help=argparse.SUPPRESS)
    up.add_argument("--no-payloads", action="store_true", help=argparse.SUPPRESS)
    up.add_argument("--no-png", action="store_true", help=argparse.SUPPRESS)
    up.add_argument("--no-sprites", action="store_true", help=argparse.SUPPRESS)
    up.add_argument("--no-audio", action="store_true", help=argparse.SUPPRESS)
    up.add_argument("--no-model-glb", action="store_true", help=argparse.SUPPRESS)
    up.add_argument("--no-reconstructed-glb", action="store_true", help=argparse.SUPPRESS)
    up.add_argument(
        "--reconstructed-kind",
        action="append",
        choices=("scene", "prefab", "terrain", "navmesh"),
        help="only reconstruct this GLB kind, can be repeated",
    )
    up.add_argument("--reconstructed-limit", type=int, help="stop after this many reconstructed GLB exports")
    up.add_argument("--no-mono-json", action="store_true", help=argparse.SUPPRESS)
    up.add_argument("--animation-json", action="store_true", help=argparse.SUPPRESS)
    up.add_argument("--no-animation-json", action="store_true", help=argparse.SUPPRESS)
    up.add_argument("--no-animation-glb", action="store_true", help=argparse.SUPPRESS)
    up.add_argument("--no-material-json", action="store_true", help=argparse.SUPPRESS)
    up.add_argument("--no-fonts", action="store_true", help=argparse.SUPPRESS)
    up.add_argument("--no-shaders", action="store_true", help=argparse.SUPPRESS)
    up.add_argument("--no-textmesh-json", action="store_true", help=argparse.SUPPRESS)
    up.add_argument("--no-ui-json", action="store_true", help=argparse.SUPPRESS)
    up.add_argument(
        "--no-map-component-json",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    up.add_argument(
        "--no-editable-asset-json",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    up.add_argument(
        "--gameobject-json",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    up.add_argument("--no-taildata", action="store_true", help="skip external sunflowyr_taildata.json metadata")
    up.add_argument("--all-objects", action="store_true", help=argparse.SUPPRESS)
    up.add_argument("--full-objects", action="store_true", help=argparse.SUPPRESS)
    up.add_argument("--class-id", action="append", type=int, help="only process this Unity class id, repeatable")
    up.add_argument("--file", action="append", help="only process this serialized file name, can be repeated")
    up.add_argument("--limit", type=int, help="stop after this many exports")
    up.add_argument(
        "--jobs",
        type=int,
        help="worker processes for conversion work, default picks from cores and free memory, 1 disables",
    )
    up.add_argument("--manifest-jsonl", action="store_true", help="also write legacy debug manifest.jsonl")
    up.add_argument("--no-manifest", action="store_true", help=argparse.SUPPRESS)
    up.set_defaults(func=cmd_unpack)

    from_glb = sub.add_parser(
        "prefab-from-glb",
        help="turn an edited reconstructed scene/prefab GLB back into a stageable prefab folder",
    )
    from_glb.add_argument("glb", type=Path, help="reconstructed scene or prefab GLB")
    from_glb.add_argument("--out", required=True, type=Path, help="output folder to stage")
    from_glb.add_argument("--name", help="prefab name; defaults to the GLB root node")
    from_glb.add_argument(
        "--data",
        type=Path,
        default=default_data_root(),
        help="rwby-ge_Data folder/ lets materials, textures and shaders be rebuilt from the game",
    )
    from_glb.add_argument(
        "--geometry-only",
        action="store_true",
        help="skip rebuilding the original prefab. emit only the GLB's meshes and hierarchy",
    )
    from_glb.add_argument(
        "--follow-refs",
        type=int,
        default=0,
        help="how many levels of referenced prefabs to pull in when rebuilding, default is 0",
    )
    from_glb.add_argument(
        "--no-materials",
        action="store_true",
        help="skip rebuilding materials from the game, renderer slots stay empty",
    )
    from_glb.add_argument(
        "--game-refs",
        action="store_true",
        help="point references to objects outside the prefab at the game objects instead of dropping them (loose mods)",
    )
    from_glb.set_defaults(func=cmd_prefab_from_glb)

    navmesh_glb = sub.add_parser(
        "navmesh-from-glb",
        help="turn an edited navmesh GLB back into a stageable NavMeshData folder",
    )
    navmesh_glb.add_argument("glb", type=Path, help="exported navmesh GLB, edited in Blender")
    navmesh_glb.add_argument("--out", required=True, type=Path, help="output folder to stage")
    navmesh_glb.add_argument(
        "--data",
        type=Path,
        default=default_data_root(),
        help="rwby-ge_Data folder. the original navmesh is read from it for its parameters",
    )
    navmesh_glb.set_defaults(func=cmd_navmesh_from_glb)

    tail = sub.add_parser("taildata", help="read a Sunflowyr taildata catalog")
    tail.add_argument("path", type=Path)
    tail.set_defaults(func=cmd_taildata)

    bundle = sub.add_parser("bundle-extract", help="extract UnityFS/UnityWeb AssetBundle contents")
    bundle.add_argument("path", type=Path, help="Unity AssetBundle file")
    bundle.add_argument("--out", type=Path, help="output folder")
    bundle.set_defaults(func=cmd_bundle_extract)

    bundle_build = sub.add_parser(
        "bundle-build",
        help="build a Unity 5.4.1p4 RWBY GE new-asset AssetBundle",
    )
    bundle_build.add_argument("--name", required=True, help="AssetBundle name")
    bundle_build.add_argument("--out", required=True, type=Path, help="output AssetBundle file")
    bundle_build.add_argument(
        "--data",
        type=Path,
        default=default_data_root(),
        help="rwby-ge_Data folder; its Managed assemblies lay out MonoBehaviour/definition fields",
    )
    bundle_build.add_argument(
        "--input",
        action="append",
        dest="inputs",
        required=True,
        type=Path,
        help=(
            "supported audio, font, GLB mesh, .sanim AnimationClip, image (optional .suntex sidecar for "
            "DXT/mip settings), material JSON, sprite JSON, physics JSON, text/data file, or folder; repeatable"
        ),
    )
    bundle_build.add_argument(
        "--rebind",
        action="append",
        dest="rebind",
        default=[],
        type=Path,
        help=(
            "Sunflowyr-exported original that a staged asset should rebind at runtime, "
            "matched via taildata and written into the .bundle.json sidecar. repeatable"
        ),
    )
    bundle_build.add_argument(
        "--rebind-targets",
        type=Path,
        help="rebind targets JSON naming game assets by name",
    )
    bundle_build.add_argument(
        "--taildata",
        action="append",
        dest="taildata",
        default=[],
        type=Path,
        help="taildata catalog for --rebind files when it's not beside them, repeatable",
    )
    bundle_build.add_argument(
        "--rebind-scan-mono",
        action="store_true",
        help="also scan typed MonoBehaviour fields when building the embedded rebind map",
    )
    bundle_build.set_defaults(func=cmd_bundle_build)

    clone = sub.add_parser(
        "prefab-clone",
        help="export a complete game character as a stageable prefab folder",
    )
    clone.add_argument("--data", type=Path, default=default_data_root(), help="rwby-ge_Data folder")
    clone.add_argument("--character", help="character root GameObject name, e.g. Ruby")
    clone.add_argument("--out", type=Path, help="output folder for the prefab and its assets")
    clone.add_argument("--list", action="store_true", help="list character roots and exit")
    clone.add_argument(
        "--no-scripts",
        dest="scripts",
        action="store_false",
        help="omit the character's MonoBehaviour components",
    )
    clone.add_argument(
        "--follow-refs",
        type=int,
        default=1,
        help="how many levels of referenced prefabs to pull in (-1 = follow everything)",
    )
    clone.add_argument(
        "--minimal",
        action="store_true",
        help="clone only this character: no referenced prefabs, no data assets. smallest test bundle",
    )
    clone.add_argument(
        "--prefab-cap",
        type=int,
        default=400,
        help="safety cap on how many referenced prefabs to clone",
    )
    clone.set_defaults(func=cmd_prefab_clone, scripts=True)

    bundle_map = sub.add_parser(
        "bundle-map",
        help="build a BepInEx replacement map: scene references for assets a bundle replaces",
    )
    bundle_map.add_argument("--data", type=Path, default=default_data_root(), help="rwby-ge_Data folder")
    bundle_map.add_argument("--manifest", required=True, type=Path, help="bundle .json manifest from bundle-build")
    bundle_map.add_argument(
        "--replace",
        action="append",
        dest="replacements",
        default=[],
        type=Path,
        help="Sunflowyr-exported file or folder the bundle asset rebinds, matched via taildata. repeatable",
    )
    bundle_map.add_argument(
        "--targets",
        type=Path,
        help=(
            "JSON file naming targets by asset name instead of taildata: "
            '{"targets": [{"className": "Texture2D", "replaceName": "<game asset name>", '
            '"withAsset": "<bundle assetPath or name>"}]} needs no unpack'
        ),
    )
    bundle_map.add_argument(
        "--taildata",
        action="append",
        dest="taildata",
        type=Path,
        default=[],
        help="sunflowyr_taildata.json catalog path when it's not next to the staged files. repeatable",
    )
    bundle_map.add_argument("--out", required=True, type=Path, help="output replacement map JSON")
    bundle_map.add_argument(
        "--scan-mono",
        action="store_true",
        help="also scan typed MonoBehaviour fields. it's needed for Sprite/UI references but slower",
    )
    bundle_map.set_defaults(func=cmd_bundle_map)

    bundle_inspect = sub.add_parser(
        "bundle-inspect",
        help="validate and summarize a generated UnityFS AssetBundle",
    )
    bundle_inspect.add_argument("path", type=Path, help="Unity AssetBundle file")
    bundle_inspect.add_argument("--json", action="store_true", help="print the full JSON report")
    bundle_inspect.set_defaults(func=cmd_bundle_inspect)
    return parser

def cmd_inventory(args: argparse.Namespace) -> int:
    data = inventory(args.data)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    print(f"Data root: {data['data_root']}")
    print(f"Serialized files: {data['file_count']}")
    print(f"Objects: {data['object_count']}")
    print("Top classes:")
    for name, count in list(data["classes"].items())[:20]:
        print(f"  {name}: {count}")
    return 0

def cmd_unpack(args: argparse.Namespace) -> int:
    include_classes = set(args.class_id) if args.class_id else None
    files = {item.lower().replace("\\", "/") for item in args.file} if args.file else None
    legacy_selection = any(
        (
            args.no_raw,
            args.no_payloads,
            args.no_png,
            args.no_sprites,
            args.no_audio,
            args.no_model_glb,
            args.no_reconstructed_glb,
            args.no_mono_json,
            args.animation_json,
            args.no_animation_json,
            args.no_animation_glb,
            args.no_material_json,
            args.no_fonts,
            args.no_shaders,
            args.no_textmesh_json,
            args.no_ui_json,
            args.no_map_component_json,
            args.no_editable_asset_json,
            args.gameobject_json,
            args.all_objects,
            args.full_objects,
        )
    )
    new_selection = bool(args.raw or args.full or args.editable or args.animation_json)
    if args.full or (not new_selection and not legacy_selection):
        selected_editables = set(EDITABLE_EXPORT_CHOICES)
        raw_objects = True
        full_mode = True
    elif new_selection:
        selected_editables = set(EDITABLE_EXPORT_CHOICES) if args.full else set(args.editable or ())
        raw_objects = bool(args.raw or args.full)
        full_mode = bool(args.full)
    else:
        selected_editables = set(EDITABLE_EXPORT_CHOICES)
        raw_objects = not args.no_raw
        full_mode = bool(args.full_objects or args.all_objects)

    disabled = {
        "text-assets": args.no_payloads,
        "png": args.no_png,
        "sprites": args.no_sprites,
        "audio": args.no_audio,
        "model-glb": args.no_model_glb,
        "reconstructed-glb": args.no_reconstructed_glb,
        "mono-json": args.no_mono_json,
        "animation-json": args.no_animation_json,
        "animation-glb": args.no_animation_glb,
        "material-json": args.no_material_json,
        "fonts": args.no_fonts,
        "shaders": args.no_shaders,
        "textmesh-json": args.no_textmesh_json,
        "ui-json": args.no_ui_json,
        "map-json": args.no_map_component_json,
        "asset-data": args.no_editable_asset_json,
    }
    for name, is_disabled in disabled.items():
        if is_disabled:
            selected_editables.discard(name)
    if args.gameobject_json:
        selected_editables.add("gameobject-json")
    if args.animation_json and not args.no_animation_json:
        selected_editables.add("animation-json")
    if args.no_raw:
        raw_objects = False

    options = ExportOptions(
        data_root=args.data,
        output_root=args.out,
        raw_objects=raw_objects,
        write_taildata=not args.no_taildata and bool(selected_editables),
        all_objects=args.all_objects or raw_objects,
        include_classes=include_classes,
        files=files,
        limit=args.limit,
        manifest=bool(args.manifest_jsonl and not args.no_manifest),
        convert_png="png" in selected_editables,
        convert_sprites="sprites" in selected_editables,
        convert_audio="audio" in selected_editables,
        convert_text_assets="text-assets" in selected_editables,
        convert_model_glb="model-glb" in selected_editables,
        convert_reconstructed_glb="reconstructed-glb" in selected_editables,
        reconstructed_kinds=set(args.reconstructed_kind) if args.reconstructed_kind else None,
        reconstructed_limit=args.reconstructed_limit,
        convert_mono_json="mono-json" in selected_editables,
        convert_animation_json="animation-json" in selected_editables,
        convert_animation_package="animation" in selected_editables,
        convert_animation_glb="animation-glb" in selected_editables,
        convert_material_json="material-json" in selected_editables,
        convert_fonts="fonts" in selected_editables,
        convert_shaders="shaders" in selected_editables,
        convert_textmesh_json="textmesh-json" in selected_editables,
        convert_ui_json="ui-json" in selected_editables,
        convert_map_component_json="map-json" in selected_editables,
        convert_editable_asset_json=bool({"asset-data", "asset-json"} & selected_editables),
        convert_gameobject_json="gameobject-json" in selected_editables,
        full_objects=args.full_objects or raw_objects or full_mode,
        jobs=args.jobs,
    )
    stats = unpack(options)
    print(f"Scanned files: {stats.scanned_files}")
    print(f"Scanned objects: {stats.scanned_objects}")
    print(f"Raw object exports: {stats.raw_exports}")
    print(f"Raw object bytes: {stats.raw_object_bytes}")
    print(f"Inlined resource objects: {stats.raw_inlined_resource_objects}")
    print(f"Inlined resource bytes: {stats.raw_inlined_resource_bytes}")
    if stats.raw_inlining_errors:
        print(f"Raw inlining errors: {stats.raw_inlining_errors}")
    print(f"TextAsset exports: {stats.text_asset_exports}")
    print(f"PNG exports: {stats.png_exports}")
    print(f"Sprite PNG exports: {stats.sprite_png_exports}")
    print(f"Sprite JSON exports: {stats.sprite_json_exports}")
    print(f"Audio exports: {stats.audio_exports}")
    print(f"Mesh GLB exports: {stats.mesh_glb_exports}")
    print(f"Scene GLB exports: {stats.scene_glb_exports}")
    print(f"Prefab GLB exports: {stats.prefab_glb_exports}")
    print(f"Scene component manifests: {stats.scene_component_manifest_exports}")
    print(f"Prefab component manifests: {stats.prefab_component_manifest_exports}")
    print(f"Terrain GLB exports: {stats.terrain_glb_exports}")
    print(f"Terrain JSON exports: {stats.terrain_json_exports}")
    print(f"NavMesh GLB exports: {stats.navmesh_glb_exports}")
    print(f"NavMesh JSON exports: {stats.navmesh_json_exports}")
    print(f"MonoBehaviour JSON exports: {stats.mono_json_exports}")
    print(f"MonoScript JSON exports: {stats.monoscript_json_exports}")
    if args.animation_json or stats.animation_json_exports:
        print(f"AnimationClip JSON exports: {stats.animation_json_exports}")
    print(f"AnimationClip package exports: {stats.animation_package_exports}")
    print(f"AnimationClip GLB exports: {stats.animation_glb_exports}")
    print(f"Material JSON exports: {stats.material_json_exports}")
    print(f"Font JSON exports: {stats.font_json_exports}")
    print(f"Font file exports: {stats.font_file_exports}")
    print(f"Shader exports: {stats.shader_exports}")
    print(f"TextMesh JSON exports: {stats.textmesh_json_exports}")
    print(f"UI JSON exports: {stats.ui_json_exports}")
    print(f"Map component JSON exports: {stats.map_component_json_exports}")
    print(f"Editable asset exports: {stats.editable_asset_json_exports}")
    if stats.mono_json_typed_exports:
        print(f"Typed MonoBehaviour JSON exports: {stats.mono_json_typed_exports}")
    if stats.mono_json_raw_fallbacks:
        print(f"Raw-fallback MonoBehaviour JSON exports: {stats.mono_json_raw_fallbacks}")
    print(f"Media settings exports: {stats.media_settings_exports}")
    if stats.unsupported_png:
        print(f"Unsupported PNG conversions: {stats.unsupported_png}")
    if stats.unsupported_sprites:
        print(f"Unsupported Sprite conversions: {stats.unsupported_sprites}")
    if stats.unsupported_audio:
        print(f"Unsupported audio conversions: {stats.unsupported_audio}")
    if stats.unsupported_model_glb:
        print(f"Unsupported Mesh GLB conversions: {stats.unsupported_model_glb}")
    if stats.reconstructed_glb_errors:
        print(f"Reconstructed GLB errors: {stats.reconstructed_glb_errors}")
    if stats.unsupported_mono_json:
        print(f"Unsupported MonoBehaviour JSON exports: {stats.unsupported_mono_json}")
    if stats.unsupported_animation_json:
        print(f"Unsupported AnimationClip JSON exports: {stats.unsupported_animation_json}")
    if stats.unsupported_animation_package:
        print(f"Unsupported AnimationClip package exports: {stats.unsupported_animation_package}")
    if stats.unsupported_animation_glb:
        print(f"Unsupported AnimationClip GLB exports: {stats.unsupported_animation_glb}")
    if stats.unsupported_material_json:
        print(f"Unsupported Material JSON exports: {stats.unsupported_material_json}")
    if stats.unsupported_fonts:
        print(f"Unsupported Font exports: {stats.unsupported_fonts}")
    if stats.unsupported_shaders:
        print(f"Unsupported Shader exports: {stats.unsupported_shaders}")
    if stats.unsupported_textmesh_json:
        print(f"Unsupported TextMesh JSON exports: {stats.unsupported_textmesh_json}")
    if stats.unsupported_ui_json:
        print(f"Unsupported UI JSON exports: {stats.unsupported_ui_json}")
    if stats.unsupported_map_component_json:
        print(f"Unsupported map component JSON exports: {stats.unsupported_map_component_json}")
    if stats.unsupported_editable_asset_json:
        print(f"Unsupported editable asset JSON exports: {stats.unsupported_editable_asset_json}")
    if stats.skipped_missing_resources:
        print(f"Missing resources skipped: {stats.skipped_missing_resources}")
    if stats.errors:
        print(f"Errors: {stats.errors}")
    if stats.raw_inlining_errors or stats.errors:
        return 1
    print(f"Output: {options.output_root.resolve()}")
    return 0

def cmd_navmesh_from_glb(args: argparse.Namespace) -> int:
    from .navmesh_from_glb import NavMeshFromGlbError, rebuild_navmesh_folder

    try:
        result = rebuild_navmesh_folder(Path(args.glb), Path(args.out), Path(args.data))
    except NavMeshFromGlbError as exc:
        print(f"conversion failed: {exc}")
        return 1
    print(f"NavMesh: {result['name']}   from {result['container']} path {result['pathId']}")
    print(
        f"Tiles:   {result['tileCount']} rebuilt (was {result['originalTileCount']}), "
        f"{result['polygonCount']} polygons (was {result['originalPolygonCount']}), "
        f"{result['vertexCount']} vertices"
    )
    print(
        f"Source:  {result['sourceTriangleCount']} triangle(s) in the GLB, "
        f"{result['byteLength']} bytes of tile data"
    )
    print(f"Stage {result['folder']} in the AssetBundle generator or --input it.")
    return 0

def cmd_prefab_from_glb(args: argparse.Namespace) -> int:
    from .prefab_from_glb import PrefabFromGlbError

    context = None
    assets = []
    if not args.no_materials:
        from .model_context import build_model_context
        from .unpack_sources import unpack_source_roots
        from .serialized import discover_serialized_files, load_serialized_asset

        data_root = Path(args.data).resolve()
        roots = [data_root] + unpack_source_roots(Path(args.glb), data_root)
        for root in roots:
            print(f"loading {root} ...")
            assets.extend(load_serialized_asset(path, root) for path in discover_serialized_files(root))
        context = build_model_context(assets)
    try:
        result = rebuild_or_convert_glb(Path(args.glb), Path(args.out), args.name, context, args, assets)
    except PrefabFromGlbError as exc:
        print(f"conversion failed: {exc}")
        return 1
    print(f"Prefab: {result['prefab']}   mode {result.get('mode', 'geometry')}")
    print(f"Nodes:  {result['nodes']}   meshes {result['meshes']}   materials {result['materials']}")
    if result.get("mode") == "clone-merge":
        print(
            f"Edits:  {result['transformsUpdated']} transforms updated from the GLB, "
            f"{len(result['meshesReplaced'])} meshes replaced, "
            f"{result['nodesMissingFromGlb']} nodes not present in the GLB kept as is"
        )
        added = result.get("nodesAdded", 0)
        renamed = result.get("nodesRenamed", 0)
        if added or renamed:
            print(
                f"        {added} node(s) added from the GLB "
                f"({result.get('meshesAdded', 0)} with geometry), {renamed} renamed"
            )
        for item in result.get("materialsUnresolved") or []:
            print(f"  no material for {item}. set it in the prefab JSON before building")
        print(f"Kept:   {result['scripts']} scripts, {result['animations']} animation(s)")
    for warning in result["warnings"][:10]:
        print(f"  warning: {warning}")
    print(f"Stage {result['folder']} in the AssetBundle generator, or --input it.")
    return 0

def rebuild_or_convert_glb(glb_path: Path, out_folder: Path, name, context, args, assets):
    from .animation_glb_import import read_glb
    from .prefab_from_glb import convert_hierarchy_glb, rebuild_prefab_from_edited_glb, root_source_key

    document, unused_binary = read_glb(glb_path.read_bytes())
    if context is not None and root_source_key(document) is not None and not getattr(args, "geometry_only", False):
        from .managed_schema import shared_registry
        from .mono_export import build_monoscript_index

        data_root = Path(args.data).resolve()
        registry = None
        try:
            registry = shared_registry(data_root / "Managed")
        except Exception:
            registry = None
        return rebuild_prefab_from_edited_glb(
            glb_path,
            out_folder,
            context,
            name,
            managed_registry=registry,
            mono_scripts=build_monoscript_index(assets),
            follow_refs=int(getattr(args, "follow_refs", 0) or 0),
            game_refs=bool(getattr(args, "game_refs", False)),
        )
    return convert_hierarchy_glb(glb_path, out_folder, name, context)

def cmd_taildata(args: argparse.Namespace) -> int:
    metadata = read_taildata(args.path)
    if metadata is None:
        print("No Sunflowyr taildata found.")
        return 1
    print(json.dumps(metadata, indent=2, ensure_ascii=False))
    return 0

def cmd_bundle_extract(args: argparse.Namespace) -> int:
    output_root = args.out or args.path.with_name(f"{args.path.name}_extracted")
    files = extract_bundle(args.path, output_root)
    print(f"Bundle files: {len(files)}")
    print(f"Output: {output_root.resolve()}")
    for path in files:
        print(f"  {path.name}: {path.stat().st_size} bytes")
    return 0

def monoscript_index_for(serialized):
    from .mono_export import build_monoscript_index

    return build_monoscript_index(serialized)

def cmd_prefab_clone(args: argparse.Namespace) -> int:
    from .model_context import build_model_context
    from .managed_schema import shared_registry
    from .mono_export import build_monoscript_index
    from .prefab_clone import (
        PrefabCloneError,
        clone_character_exact,
        describe_clone,
        find_character_roots,
        write_clone_report,
    )
    from .serialized import discover_serialized_files, load_serialized_asset

    data_root = Path(args.data).resolve()
    if args.out:
        Path(args.out).mkdir(parents=True, exist_ok=True)
    print(f"loading {data_root} ")
    serialized = [load_serialized_asset(path, data_root) for path in discover_serialized_files(data_root)]
    context = build_model_context(serialized)
    roots = find_character_roots(context)
    if args.list or not args.character:
        print(f"{len(roots)} character roots with a skinned mesh:")
        for key, name in roots:
            print(f"  {name}   [{key[0]} path {key[1]}]")
        if not args.character:
            print("\nre-run with --character <name> --out <folder>")
        return 0
    wanted = str(args.character).casefold()
    matches = [item for item in roots if item[1].casefold() == wanted]
    if not matches:
        matches = [item for item in roots if wanted in item[1].casefold()]
    if not matches:
        print(f'no character root named "{args.character}"; use --list to see the options')
        return 1
    if len(matches) > 1:
        print(f'"{args.character}" matches {len(matches)} roots:')
        for key, name in matches[:12]:
            print(f"  {name}   [{key[0]} path {key[1]}]")
        print("use the exact name")
        return 1
    if not args.out:
        print("--out <folder> is required when cloning")
        return 1
    root_key, root_name = matches[0]
    try:
        registry = shared_registry(data_root / "Managed")
        result = clone_character_exact(
            context,
            root_key,
            Path(args.out),
            include_scripts=args.scripts,
            managed_registry=registry,
            mono_scripts=monoscript_index_for(serialized),
            follow_refs=0 if args.minimal else args.follow_refs,
            prefab_cap=args.prefab_cap,
            minimal=args.minimal,
        )
    except PrefabCloneError as exc:
        print(f"clone failed: {exc}")
        return 1
    report = write_clone_report(result)
    print(f"Cloned: {describe_clone(result)}")
    print(f"Report: {report}  (every warning, nulled reference and rebind in full)")
    print(f"Prefab: {result.prefab_path}")
    print(f"Files:  {len(result.files)} written to {result.folder}")
    for warning in result.warnings[:8]:
        print(f"  warning: {warning}")
    if result.rebinds:
        print(f"Rebinds: {len(result.rebinds)} references the bundle cant carry, listed in sunflowyr_rebinds.json")
    for reference in result.external_refs[:8]:
        print(f"  outside bundle (nulled): {reference}")
    if len(result.external_refs) > 8:
        print(f"  and {len(result.external_refs) - 8} more outside-bundle references")
    print("\nStage that folder in the AssetBundle generator (or --input it) and build.")
    _ = root_name
    return 0

def cmd_bundle_map(args: argparse.Namespace) -> int:
    from .catalog import (
        collect_catalog_paths,
        collect_payload_files,
        find_catalog_record,
        load_catalog_indexes,
    )
    from .model_context import build_model_context
    from .replacement_map import (
        ReplacementMapError,
        ReplacementTarget,
        build_replacement_map,
        write_replacement_map,
    )
    from .serialized import discover_serialized_files, load_serialized_asset

    try:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"couldnt read bundle manifest: {exc}")
        return 1
    manifest_assets = manifest.get("assets")
    if not isinstance(manifest_assets, list) or not manifest_assets:
        print("bundle manifest lists no assets, run bundle-build first")
        return 1
    assets_by_source = {}
    for item in manifest_assets:
        if isinstance(item, dict) and item.get("sourceFile"):
            assets_by_source.setdefault(str(item["sourceFile"]).casefold(), item)

    name_requests: list[dict] = []
    if args.targets is not None:
        try:
            targets_document = json.loads(Path(args.targets).read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"couldnt read targets file: {exc}")
            return 1
        raw_targets = (
            targets_document.get("targets")
            if isinstance(targets_document, dict)
            else targets_document
        )
        if not isinstance(raw_targets, list) or not raw_targets:
            print('targets file must contain a non-empty "targets" list')
            return 1
        name_requests = [item for item in raw_targets if isinstance(item, dict)]

    replacement_paths = [str(path) for path in args.replacements]
    staged = collect_payload_files(replacement_paths) if replacement_paths else []
    if not staged and not name_requests:
        print("pass --targets or --replace with taildata-matched files")
        return 1
    catalogs = (
        load_catalog_indexes(
            collect_catalog_paths(replacement_paths, staged, [str(p) for p in args.taildata])
        )
        if staged
        else []
    )
    if staged and not catalogs:
        print("no sunflowyr_taildata.json was found, pass --taildata or stage files from an unpack folder")
        return 1

    targets: list[ReplacementTarget] = []
    unmatched: list[str] = []
    seen: set[tuple[str, int]] = set()
    for item in staged:
        catalog_path, record, output = find_catalog_record(item, catalogs)
        if not record:
            unmatched.append(f"{item.path.name}: no taildata record matches")
            continue
        bundle_asset = assets_by_source.get(item.path.name.casefold())
        if bundle_asset is None:
            unmatched.append(f"{item.path.name}: not an input of this bundle (manifest sourceFile mismatch)")
            continue
        container = str(record.get("appendTarget") or record.get("container") or "")
        path_id = int(record.get("pathId", 0))
        class_id = int(record.get("classId", 0))
        duplicate_key = (container.casefold(), path_id)
        if duplicate_key in seen:
            continue
        seen.add(duplicate_key)
        targets.append(
            ReplacementTarget(
                container=container,
                path_id=path_id,
                class_id=class_id,
                name=str(record.get("name") or item.path.stem),
                bundle_asset_path=str(bundle_asset.get("assetPath") or ""),
                bundle_class_name=str(bundle_asset.get("className") or ""),
            )
        )
    for line in unmatched:
        print(f"skipped: {line}")

    data_root = Path(args.data).resolve()
    print(f"loading {data_root} ...")
    serialized = [
        load_serialized_asset(path, data_root) for path in discover_serialized_files(data_root)
    ]
    context = build_model_context(serialized)
    if name_requests:
        from .replacement_map import resolve_targets_by_name

        named_targets, problems = resolve_targets_by_name(context, name_requests, manifest_assets)
        for problem in problems:
            print(f"target problem: {problem}")
        for target in named_targets:
            key = (target.container.casefold(), target.path_id)
            if key in seen:
                continue
            seen.add(key)
            targets.append(target)
    if not targets:
        print("no rebind targets could be resolved")
        return 1

    mono_scripts = None
    managed_registry = None
    if args.scan_mono:
        from .managed_schema import shared_registry
        from .mono_export import build_monoscript_index

        mono_scripts = build_monoscript_index(serialized)
        managed_registry = shared_registry(data_root / "Managed")

    try:
        document, stats = build_replacement_map(
            context,
            targets,
            bundle_name=str(manifest.get("bundleName") or ""),
            scan_mono=bool(args.scan_mono),
            mono_scripts=mono_scripts,
            managed_registry=managed_registry,
        )
    except ReplacementMapError as exc:
        print(f"replacement map error: {exc}")
        return 1
    write_replacement_map(document, Path(args.out))
    print(f"Replacement map: {args.out}")
    print(f"Targets: {len(targets)}")
    print(f"Scene entries: {stats.entries}")
    print(f"Unanchored targets: {stats.unanchored}")
    if args.scan_mono:
        print(f"Typed MonoBehaviours scanned: {stats.mono_objects_scanned}")
    print("Stage the map JSON into the bundle as a TextAsset or ship it next to the .bundle file.")
    return 0

def cmd_bundle_build(args: argparse.Namespace) -> int:
    try:
        result = generate_asset_bundle(
            args.out,
            args.name,
            args.inputs,
            data_root=getattr(args, "data", None),
            rebind_spec={
                "replace": getattr(args, "rebind", []) or [],
                "taildata": getattr(args, "taildata", []) or [],
                "targetsFile": getattr(args, "rebind_targets", None),
                "scanMono": getattr(args, "rebind_scan_mono", False),
            },
        )
    except (AssetBundleInputError, AssetBundleWriteError, OSError) as exc:
        print(f"AssetBundle error: {exc}")
        return 1
    print(f"Bundle: {result.output_path}")
    print(f"Manifest: {result.manifest_path}")
    print(f"Bundle name: {result.bundle_name}")
    print(f"CAB name: {result.cab_name}")
    print(f"Assets: {len(result.assets)}")
    print(f"Serialized bytes: {result.serialized_size}")
    print(f"Resource bytes: {result.resource_size}")
    print(f"Bundle bytes: {result.bundle_size}")
    log_path = write_bundle_report(result)
    print(f"Report: {log_path}  (asset inventory, registrations and rebinds in full)")
    return 0

def write_bundle_report(result) -> Path:
    import collections

    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    lines: list[str] = []
    add = lines.append
    add("Sunflowyr AssetBundle report")
    add("=" * 72)
    add(f"bundle        : {result.output_path}")
    add(f"manifest      : {result.manifest_path}")
    add(f"bundle name   : {result.bundle_name}")
    add(f"CAB name      : {result.cab_name}")
    add(f"assets        : {len(result.assets)}")
    add(f"bundle bytes  : {result.bundle_size}")
    add(f"sha256        : {manifest.get('bundleSha256', '')}")
    add("")

    add("ASSETS BY CLASS")
    add("-" * 72)
    for name, count in collections.Counter(
        item["className"] for item in result.assets
    ).most_common():
        add(f"  {count:6}x  {name}")
    add("")

    registrations = manifest.get("registrations") or []
    add(f"REGISTRATIONS ({len(registrations)})")
    add("-" * 72)
    for entry in registrations or []:
        add(f"  {entry.get('database','')} <- {entry.get('assetPath','')} [{entry.get('member','')}]")
    if not registrations:
        add("  none")
    add("")

    rebinds = manifest.get("rebinds") or []
    add(f"REBINDS ({len(rebinds)})")
    add("-" * 72)
    pairs = collections.Counter(
        (str(entry.get("type", "")).split(".")[-1], str(entry.get("name", ""))) for entry in rebinds
    )
    for (kind, name), count in pairs.most_common():
        add(f"  {count:6}x  {kind:32} {name}")
    if not rebinds:
        add("  none")
    add("")
    add("FULL REBIND LIST")
    add("-" * 72)
    for entry in rebinds:
        add(
            f"  {entry.get('assetPath') or entry.get('asset','')} :: {entry.get('script','')}."
            f"{entry.get('memberPath','')} -> {entry.get('name','')} ({entry.get('type','')})"
        )
    if not rebinds:
        add("  none")
    add("")

    add("FULL ASSET LIST")
    add("-" * 72)
    for item in result.assets:
        add(f"  {item['className']:22} {item['assetPath']}")

    log_path = Path(str(result.output_path) + ".build.log")
    log_path.write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
    return log_path

def cmd_bundle_inspect(args: argparse.Namespace) -> int:
    try:
        report = inspect_asset_bundle(args.path)
    except (AssetBundleInputError, ValueError, OSError) as exc:
        print(f"AssetBundle error: {exc}")
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(f"Bundle: {report['path']}")
    print(f"Bundle bytes: {report['bundleSize']}")
    print(f"Unity: {report['unityVersion']}")
    print(f"Serialized format: {report['serializedFormatVersion']}")
    print(f"Platform: {report['platform']}")
    print(f"Objects: {report['objectCount']}")
    print(f"Classes: {report['classCounts']}")
    print("Assets:")
    for name in report["assetNames"]:
        print(f"  {name}")
    print("Nodes:")
    for node in report["nodes"]:
        print(f"  {node['path']}: {node['size']} bytes, flags={node['flags']}")
    return 0

def default_data_root() -> Path:
    candidates = [
        Path.cwd() / "rwby-ge_Data",
        Path(__file__).resolve().parent.parent / "rwby-ge_Data",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]
