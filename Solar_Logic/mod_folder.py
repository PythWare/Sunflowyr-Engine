from __future__ import annotations
import json, mmap, re, shutil, tempfile, zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from .asset_bundle_writeback import WritebackAssetError, build_writeback_asset
from .catalog import CatalogIndex, StagedPayload, find_catalog_record, path_id_from_name, unique_target_matches
from .class_ids import class_name
from .loose_format import (
    MOD_INFO,
    PREVIEW_FOLDER,
    ModInfo,
    copy_mod_extras,
    preview_images,
    read_mod_info,
    write_mod_info,
    MOD_LIST,
    MOD_MANIFEST,
    MODS_FOLDER,
    PAYLOAD_SUFFIX,
    UNSAFE_NAME,
    LooseFormatError,
    LooseObject,
    ModEntry,
    ModObject,
    active_overrides,
    container_folder,
    ensure_listed,
    mods_root,
    payload_path_id,
    read_mod_list,
    read_mod_objects,
    safe_mod_name,
    simulate,
    write_loose_mod,
    write_mod_list,
)
from .loose_assets import IDS_FILE, scene_glb_container
from .media_settings import is_media_sidecar
from .serialized import is_serialized_file
from .taildata import BUILD_FOLDER, EDITS_FOLDER, EXTERNAL_TAILDATA_FILENAME, is_work_folder

LOADER_LOG = "sunflowyr_loader.log"
LOADER_NAME = "version.dll"
LOADER_MARKER = b"SunflowyrLoader"
LOADER_BUILD = Path(__file__).resolve().parent / "dll" / LOADER_NAME
OTHER_PROXY_SUFFIX = ".sunflowyr_off"
SMALL_CATALOG_BYTES = 64 * 1024 * 1024

class ModFolderError(LooseFormatError):
    pass

@dataclass(slots=True)
class SourceFile:
    path: Path
    relative: str
    record: dict[str, Any] | None = None
    catalog_key: str = ""
    state: str = "unmatched"
    message: str = ""
    size: int = 0
    added_id: int = 0
    added_class: int = 0
    added_container: str = ""
    matched_path: Path | None = None

    @property
    def is_addition(self) -> bool:
        return self.path.name.lower().endswith(ADDITION_SUFFIX)

    @property
    def match_paths(self) -> list[Path]:
        if self.is_addition:
            stem = self.path.name[: -len(ADDITION_SUFFIX)]
            return [self.path.with_name(stem + extension) for extension in ADDITION_TARGETS]
        return [self.path]

    @property
    def match_path(self) -> Path:
        return self.matched_path or self.match_paths[0]

    @property
    def container(self) -> str:
        return str((self.record or {}).get("container") or "")

    @property
    def path_id(self) -> int:
        return int((self.record or {}).get("pathId") or 0)

    @property
    def class_id(self) -> int:
        return int((self.record or {}).get("classId") or 0)

    @property
    def object_name(self) -> str:
        return str((self.record or {}).get("name") or "")

@dataclass(slots=True)
class ModCheck:
    applied: list[ModObject] = field(default_factory=list)
    missing: list[ModObject] = field(default_factory=list)
    overridden: list[tuple[ModObject, ModObject]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

def payload_name(source: SourceFile) -> str:
    stem = source.path.stem
    if path_id_from_name(stem) != source.path_id:
        stem = f"{source.object_name or 'object'}__path{source.path_id}"
    stem = UNSAFE_NAME.sub("_", stem).strip(". ") or f"object__path{source.path_id}"
    return stem + PAYLOAD_SUFFIX

def is_inside(path: Path, folder: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(folder).resolve())
        return True
    except ValueError:
        return False

def source_files(folder: Path) -> list[Path]:
    found: list[Path] = []
    for path in sorted(Path(folder).rglob("*"), key=lambda item: item.as_posix().lower()):
        parts = path.relative_to(folder).parts
        if not path.is_file() or any(is_work_folder(part) for part in parts[:-1]) or parts[-1].startswith(".") or parts[0].lower() == PREVIEW_FOLDER:
            continue
        if path.name in (EXTERNAL_TAILDATA_FILENAME, MOD_MANIFEST, IDS_FILE) or (len(parts) == 1 and path.name.lower() == MOD_INFO) or is_media_sidecar(path):
            continue
        found.append(path)
    return found

ADDITION_SUFFIX = ".add.json"
ADDITION_TARGETS = (".json", ".sqlite")
IGNORED_NAMES = ("readme", "license", "licence", "changelog", "credits")
COMPANION_SUFFIXES = (".tiles.bin", ".u8.bin", ".u16le.bin", ".f32le.bin", ".pvs.bin", "_collision.bin", ".components.json", ".settings.json", ".suntex", ".shader.deps.json")

def is_companion_record(record: dict[str, Any]) -> bool:
    kind = str(record.get("kind") or "")
    return bool(record.get("binaryRole") or record.get("binary_role")) or kind.endswith("_binary") or kind.endswith("_component_manifest")

def is_new_asset_file(path: Path) -> bool:
    from .asset_bundle_inputs import SUPPORTED_EXTENSIONS

    lowered = Path(path).name.lower()
    if any(lowered.startswith(prefix) for prefix in IGNORED_NAMES) or "__path" in lowered or lowered.endswith(COMPANION_SUFFIXES):
        return False
    return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS

def nearest_catalog(folder: Path) -> Path | None:
    for candidate in [Path(folder).resolve(), *Path(folder).resolve().parents]:
        catalog = candidate / EXTERNAL_TAILDATA_FILENAME
        if catalog.is_file():
            return catalog
    return None

def catalog_index(path: Path, records: dict[str, Any]) -> CatalogIndex:
    basename_records: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    stem_records: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    path_id_records: dict[int, list[tuple[str, dict[str, Any]]]] = {}
    for key, value in records.items():
        if not isinstance(value, dict):
            continue
        basename = Path(key).name.lower()
        basename_records.setdefault(basename, []).append((key, value))
        stem_records.setdefault(Path(basename).stem, []).append((key, value))
        path_id = path_id_from_name(Path(basename).stem)
        if path_id is not None:
            path_id_records.setdefault(path_id, []).append((key, value))
    return CatalogIndex(Path(path).resolve(), Path(path).resolve().parent, records, basename_records, stem_records, path_id_records)

def read_small_catalog(path: Path) -> dict[str, Any]:
    try:
        catalog = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    files = catalog.get("files") if isinstance(catalog, dict) else None
    return files if isinstance(files, dict) else {}

def source_fingerprint(folder: Path, output_root: Path | None) -> tuple:
    entries = []
    for path in source_files(folder):
        stat = path.stat()
        entries.append((path.relative_to(folder).as_posix(), stat.st_size, stat.st_mtime_ns))
    for catalog in (Path(folder) / EXTERNAL_TAILDATA_FILENAME, nearest_catalog(Path(folder).parent), Path(output_root) / EXTERNAL_TAILDATA_FILENAME if output_root else None):
        if catalog is not None and catalog.is_file():
            stat = catalog.stat()
            entries.append((str(catalog), stat.st_size, stat.st_mtime_ns))
    return tuple(entries)

CATALOG_KEY = re.compile(rb'\n    "((?:[^"\\\n]|\\.)*)": \{')
PATH_ID_TEXT = re.compile(rb"__path(\d+)")
ID_ALTERNATION_LIMIT = 64
STEM_SEARCH_LIMIT = 8

def catalog_record(data: mmap.mmap, raw: bytes, body_start: int, wanted: Callable[[str], bool]) -> tuple[str, Any] | None:
    text = raw.decode("utf-8") if b"\\" not in raw else json.loads(b'"' + raw + b'"')
    if "/" not in text or not wanted(text):
        return None
    close = data.find(b"\n    }", body_start)
    if close < 0:
        return None
    try:
        return text, json.loads(b"{" + data[body_start:close] + b"\n}")
    except ValueError:
        return None

def key_line(data: mmap.mmap, at: int) -> int | None:
    line = data.rfind(b"\n", 0, at)
    return line if data[line + 1 : line + 6] == b'    "' else None

def search_catalog_records(path: Path, path_ids: set[int], stems: set[bytes], wanted: Callable[[str], bool], progress: Callable[[float], None] | None = None) -> dict[str, Any]:
    records: dict[str, Any] = {}

    def keep(data: mmap.mmap, match: re.Match | None) -> None:
        if match is not None:
            found = catalog_record(data, match.group(1), match.end(), wanted)
            if found is not None:
                records[found[0]] = found[1]

    with Path(path).open("rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
        if len(stems) > STEM_SEARCH_LIMIT:
            total = max(1, len(data))
            for count, match in enumerate(CATALOG_KEY.finditer(data)):
                if progress is not None and count & 0x3FFF == 0:
                    progress(match.start() / total)
                keep(data, match)
        else:
            starts: set[int] = set()
            if path_ids and len(path_ids) <= ID_ALTERNATION_LIMIT:
                pattern = re.compile(rb"__path(?:" + b"|".join(str(value).encode("ascii") for value in sorted(path_ids)) + rb")(?!\d)")
                hits = (match.start() for match in pattern.finditer(data))
            elif path_ids:
                hits = (match.start() for match in PATH_ID_TEXT.finditer(data) if int(match.group(1)) in path_ids)
            else:
                hits = iter(())
            for at in hits:
                line = key_line(data, at)
                if line is not None:
                    starts.add(line)
            for index, stem in enumerate(sorted(stems)):
                at = data.find(stem)
                while at != -1:
                    line = key_line(data, at)
                    if line is not None:
                        starts.add(line)
                    at = data.find(stem, at + 1)
                if progress is not None:
                    progress((index + 1) / max(1, len(stems)))
            for line in sorted(starts):
                keep(data, CATALOG_KEY.match(data, line))
    if progress is not None:
        progress(1.0)
    return records

def catalog_needles(items: list[SourceFile]) -> tuple[set[int], set[bytes]]:
    path_ids: set[int] = set()
    stems: set[bytes] = set()
    for item in items:
        for path in item.match_paths:
            path_id = path_id_from_name(path.stem)
            if path_id is not None:
                path_ids.add(path_id)
                continue
            for stem in {path.stem, path.stem.lower()}:
                stems.add(b"/" + json.dumps(stem, ensure_ascii=False)[1:-1].encode("utf-8") + b".")
    return path_ids, stems

def scan_source(folder: Path, output_root: Path | None, progress: Callable[[float], None] | None = None) -> list[SourceFile]:
    folder = Path(folder)
    if not folder.is_dir():
        raise ModFolderError(f"{folder} isnt a folder")
    files = [SourceFile(path, path.relative_to(folder).as_posix(), size=path.stat().st_size) for path in source_files(folder)]
    if not files:
        return files
    indexes: list[CatalogIndex] = []
    local = folder / EXTERNAL_TAILDATA_FILENAME
    if local.is_file():
        indexes.append(catalog_index(local, read_small_catalog(local)))
    big_catalogs: list[Path] = []
    nearest = nearest_catalog(folder.parent) if not local.is_file() else None
    for catalog in (nearest, Path(output_root) / EXTERNAL_TAILDATA_FILENAME if output_root else None):
        if catalog is None or not catalog.is_file() or any(index.path == catalog.resolve() for index in indexes) or catalog.resolve() in big_catalogs:
            continue
        if catalog.stat().st_size <= SMALL_CATALOG_BYTES:
            indexes.append(catalog_index(catalog, read_small_catalog(catalog)))
        else:
            big_catalogs.append(catalog.resolve())
    pending = [item for item in files if not resolve_source(item, folder, indexes)]
    for item in pending:
        if path_id_from_name(item.match_path.stem) is None and "__path" not in item.path.name.lower():
            scene = scene_glb_container(item.path)
            if scene is not None:
                item.record = {"kind": "reconstructed_scene_glb", "container": scene, "pathId": 0, "classId": 0, "name": item.path.stem}
                item.state = "matched"
                item.message = ""
                continue
            if is_new_asset_file(item.path):
                item.state = "new"
                item.message = "New asset"
            else:
                item.message = "Not a file the game can use, so its left out"
    pending = [item for item in pending if not item.message and item.record is None]
    for catalog in big_catalogs:
        if not pending:
            break
        names = {path.name.lower() for item in pending for path in item.match_paths}
        stems = {item.match_path.stem.lower() for item in pending}
        path_ids = {path_id_from_name(item.match_path.stem) for item in pending} - {None}
        def wanted(key: str) -> bool:
            basename = key.rsplit("/", 1)[-1].lower()
            stem = basename.rsplit(".", 1)[0]
            return basename in names or stem in stems or path_id_from_name(stem) in path_ids
        records = search_catalog_records(catalog, *catalog_needles(pending), wanted, progress)
        subset = catalog_index(catalog, records)
        pending = [item for item in pending if not resolve_source(item, folder, [subset])]
    for item in files:
        if item.record is not None and is_companion_record(item.record):
            item.state = "companion"
            item.message = "Companion data, rebuilt with the file it belongs to"
        if item.record is None and item.state != "new":
            item.state = "unmatched"
            item.message = item.message or "Not found in the unpack, so its left out"
    return files

def resolve_source(item: SourceFile, folder: Path, indexes: list[CatalogIndex]) -> bool:
    if not indexes:
        return False
    for candidate in item.match_paths:
        catalog_path, record, key = find_catalog_record(StagedPayload(candidate, folder.resolve()), indexes)
        if isinstance(record, dict):
            item.matched_path = candidate
            break
    if not isinstance(record, dict):
        hits = unique_target_matches([(index, name, value) for index in indexes for name, value in index.basename_records.get(item.path.name.lower(), [])])
        if len(hits) > 1:
            item.message = f"{len(hits)} game objects share this name, keep the unpack folders around it"
        return False
    item.record = record
    item.catalog_key = key
    item.state = "matched"
    item.message = ""
    return True

def save_source_catalog(folder: Path, sources: list[SourceFile], output_root: Path | None) -> Path | None:
    folder = Path(folder)
    if output_root is not None and is_inside(folder, output_root):
        return None
    matched = {Path(item.relative).with_name(item.match_path.name).as_posix(): item.record for item in sources if item.record is not None}
    if not matched:
        return None
    target = folder / EXTERNAL_TAILDATA_FILENAME
    existing = read_small_catalog(target) if target.is_file() else {}
    if all(existing.get(key) == value for key, value in matched.items()):
        return target
    existing.update(matched)
    document = {
        "format": "sunflowyr-taildata",
        "version": 1,
        "game": "rwby-ge",
        "tool": "Sunflowyr Engine",
        "taildataMode": "external-json",
        "taildataNote": "Records copied from the unpack by the Loose Builder so this mod folder can be rebuilt without it.",
        "files": existing,
    }
    target.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return target

AUDIO_KINDS = {"audio_ogg", "audio_wav"}

def build_audio_source(item: SourceFile, game_data: Path) -> LooseObject:
    from .audio_repack import load_audio_asset, resolve_audio_resource, repack_audio_view

    replacement = repack_audio_view(item.path, item.record, game_data)
    asset = load_audio_asset(game_data, item.container)
    host = resolve_audio_resource(game_data, asset, replacement.resource_container)
    try:
        host_name = host.resolve().relative_to(game_data.resolve()).as_posix()
    except ValueError as exc:
        raise ModFolderError(f"its sound data lives outside the game data folder ({replacement.resource_container})") from exc
    return LooseObject(item.container, item.path_id, replacement.object_payload, item.class_id, item.object_name,
                       resource=(replacement.resource_offset_field_offset, host_name), resource_data=replacement.resource_payload,
                       source=item.relative, file_stem=Path(payload_name(item)).stem)

def build_sprite_sources(items: list[SourceFile], game_data: Path, targets: dict[tuple[str, int], SourceFile]) -> list[tuple[SourceFile, LooseObject]]:
    if not items:
        return []
    from .sprite_repack import SpriteRepackError, prepare_sprite_png, repack_sprite_png_group

    groups: dict[tuple[str, int], list[tuple[SourceFile, Any]]] = {}
    for item in items:
        try:
            edit = prepare_sprite_png(item.path, item.record, game_data)
        except (SpriteRepackError, OSError, ValueError) as exc:
            item.state = "failed"
            item.message = str(exc)
            continue
        groups.setdefault(edit.target_key, []).append((item, edit))
    results: list[tuple[SourceFile, LooseObject]] = []
    for key, members in groups.items():
        edits = [edit for item, edit in members]
        try:
            payload, details = repack_sprite_png_group(edits)
        except (SpriteRepackError, OSError, ValueError) as exc:
            for item, edit in members:
                item.state = "failed"
                item.message = f"Sprite atlas rebuild failed: {exc}"
            continue
        first = edits[0]
        container = first.texture_asset.relative_path
        if (container.lower(), first.texture_object.path_id) in targets:
            for item, edit in members:
                item.state = "failed"
                item.message = "Another file in this folder already replaces the atlas texture this sprite lives on"
            continue
        targets[(container.lower(), first.texture_object.path_id)] = members[0][0]
        name = str(details.get("name") or f"atlas{first.texture_object.path_id}")
        loose = LooseObject(container, first.texture_object.path_id, payload, first.texture_object.class_id, name, source=members[0][0].relative, file_stem=f"{name}__path{first.texture_object.path_id}")
        for index, (item, edit) in enumerate(members):
            results.append((item, loose if index == 0 else None))
    return results

def build_new_files(folder: Path, sources: list[SourceFile], game_data: Path, mod_name: str, output_root: Path | None) -> Any:
    from .asset_bundle_inputs import AssetBundleInputError
    from .loose_assets import NewAssets, build_new_assets, convert_hierarchy_glbs, is_hierarchy_glb_file

    fresh = [item for item in sources if item.state == "new"]
    if not fresh:
        return NewAssets()
    build_root = folder / BUILD_FOLDER
    aliases: dict[str, Path] = {}
    hierarchies = [item for item in fresh if is_hierarchy_glb_file(item.path)]
    inputs: dict[int, Path] = {id(item): item.path for item in fresh if item not in hierarchies}
    if output_root is not None and is_inside(folder, output_root):
        build_root = Path(tempfile.gettempdir()) / BUILD_FOLDER / f"{zlib.crc32(str(folder.resolve()).lower().encode('utf-8')):08x}"
        mirror = build_root / "files"
        if mirror.exists():
            shutil.rmtree(mirror)
        for item in fresh:
            if id(item) not in inputs:
                continue
            copy = mirror / item.path.relative_to(folder)
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item.path, copy)
            aliases[str(copy.resolve()).lower()] = item.path
            inputs[id(item)] = copy
    converted, problems = convert_hierarchy_glbs([item.path for item in hierarchies], folder, game_data, build_root)
    for problem in problems:
        name = problem.split(":", 1)[0]
        for item in hierarchies:
            if item.path.name == name:
                item.state = "failed"
                item.message = problem.split(": ", 1)[-1]
    if any(item.state == "failed" for item in fresh):
        return None
    for item, out in zip(hierarchies, converted):
        inputs[id(item)] = out
    groups: dict[str, list[SourceFile]] = {}
    for item in fresh:
        groups.setdefault(target_game_file(folder, item.path, game_data), []).append(item)
    result = NewAssets()
    for target, members in groups.items():
        try:
            part = build_new_assets([inputs[id(item)] for item in members], folder, mod_name, game_data, target=target, output_root=output_root, build_root=build_root, aliases=aliases)
        except (AssetBundleInputError, LooseFormatError, OSError, ValueError) as exc:
            text = str(exc)
            owner = next((item for item in members if text.startswith(item.path.name)), None)
            for item in [owner] if owner is not None else members:
                item.state = "failed"
                item.message = text.split(": ", 1)[-1] if owner is not None else f"{text} (new files in {target})"
            return None
        result.objects.extend(part.objects)
        result.by_file.update(part.by_file)
        result.notes.extend(part.notes)
    for item, out in zip(hierarchies, converted):
        prefab = next(iter(sorted(out.glob("*.prefab.json"))), None)
        if prefab is not None and prefab.name.lower() in result.by_file:
            result.by_file[item.path.name.lower()] = result.by_file[prefab.name.lower()]
    for item in fresh:
        found = result.by_file.get(item.path.name.lower())
        if found is not None:
            item.added_id, item.added_class, unused, item.added_container = found
    return result

def build_hierarchy_source(item: SourceFile, folder: Path, game_data: Path, mod_name: str, output_root: Path | None, scratch: Path, kind: str) -> tuple[list[LooseObject], str]:
    from .glb_edits import edit_hierarchy
    from .loose_assets import LooseIds, build_new_assets

    edits = folder / EDITS_FOLDER / item.relative

    def allocate(key: str, count: int, container: str) -> int:
        ids = LooseIds(folder, mod_name, game_data, container)
        start = ids.block(edits / key.rsplit("#", 1)[-1], count)
        ids.save()
        return start

    def build_added(files: list[Path], container: str) -> tuple[list[LooseObject], dict[str, tuple]]:
        aliases = {str(Path(path).resolve()).lower(): edits / Path(path).parent.name / Path(path).name for path in files}
        part = build_new_assets(files, folder, mod_name, game_data, target=container, output_root=output_root, build_root=scratch / "build", aliases=aliases)
        for piece in part.objects:
            piece.source = item.relative
        return part.objects, part.by_file

    result = edit_hierarchy(item.path, game_data, scratch, item.relative, allocate, build_added, kind == "reconstructed_scene_glb")
    return result.objects, result.summary()

def target_game_file(folder: Path, path: Path, game_data: Path) -> str:
    from .loose_assets import DEFAULT_TARGET

    try:
        parts = Path(path).relative_to(folder).parts
    except ValueError:
        return DEFAULT_TARGET
    for part in parts[:-1]:
        candidate = Path(game_data) / part
        if candidate.is_file() and is_serialized_file(candidate):
            return part
    return DEFAULT_TARGET

def build_source(folder: Path, sources: list[SourceFile], game_data: Path, *, progress: Callable[[float], None] | None = None, output_root: Path | None = None) -> tuple[Path, list[ModObject]]:
    from .asset_bundle_inputs import AssetBundleInputError
    from .loose_assets import HIERARCHY_KINDS, LooseAssetError, Placeholders, build_list_additions, build_navmesh_source, dropped_references, resolve_name_references, substitute_name_references

    game_data = Path(game_data)
    folder = Path(folder)
    mod_name = safe_mod_name(folder.name)
    matched = [item for item in sources if item.record is not None and item.state != "companion"]
    if not matched and not any(item.state == "new" for item in sources):
        raise ModFolderError("nothing in this folder came from the unpack or is a new asset")
    for item in matched:
        item.state = "matched"
        item.message = ""
    new_assets = build_new_files(folder, sources, game_data, mod_name, output_root)
    if new_assets is None:
        raise ModFolderError("some new files couldnt be built, nothing was written")
    built: list[tuple[SourceFile, LooseObject]] = []
    targets: dict[tuple[str, int], SourceFile] = {}
    staging = folder / BUILD_FOLDER / "references"
    summaries: dict[int, str] = {}
    sprites = [item for item in matched if str(item.record.get("kind") or "") == "sprite_png"]
    built.extend(build_sprite_sources(sprites, game_data, targets))
    for position, item in enumerate(matched):
        if progress is not None:
            progress(position / max(1, len(matched)))
        if item in sprites:
            continue
        key = (item.container.lower(), item.path_id)
        if key in targets:
            item.state = "failed"
            item.message = f"{targets[key].relative} already replaces this object"
            continue
        kind = str(item.record.get("kind") or "")
        if kind in HIERARCHY_KINDS:
            try:
                pieces, summary = build_hierarchy_source(item, folder, game_data, mod_name, output_root, staging / "hierarchy" / item.path.stem, kind)
            except (LooseFormatError, AssetBundleInputError, WritebackAssetError, OSError, ValueError, KeyError) as exc:
                item.state = "failed"
                item.message = f"GLB edit failed: {exc}"
                continue
            clash = next((piece for piece in pieces if (piece.container.lower(), piece.path_id) in targets), None)
            if clash is not None:
                item.state = "failed"
                item.message = f"{targets[(clash.container.lower(), clash.path_id)].relative} already changes {clash.name or clash.path_id}"
                continue
            for piece in pieces:
                targets[(piece.container.lower(), piece.path_id)] = item
                built.append((item, piece))
            summaries[id(item)] = summary
            continue
        if item.is_addition:
            try:
                additions = build_list_additions(item.path, item.record, game_data, new_assets, output_root, staging / "lists")
            except (LooseAssetError, WritebackAssetError, OSError, ValueError, KeyError) as exc:
                item.state = "failed"
                item.message = f"List addition failed: {exc}"
                continue
            stem = Path(payload_name(item)).stem
            stem = stem[:-4] if stem.lower().endswith(".add") else stem
            loose = LooseObject(item.container, item.path_id, None, item.class_id, item.object_name, source=item.relative, file_stem=stem, additions=additions)
            key = (item.container.lower(), item.path_id, "add")
        elif kind in AUDIO_KINDS:
            try:
                loose = build_audio_source(item, game_data)
            except Exception as exc:
                item.state = "failed"
                item.message = f"Audio rebuild failed: {exc}"
                continue
        elif kind == "reconstructed_navmesh_glb":
            try:
                loose = build_navmesh_source(item.path, item.record, folder, game_data)
            except (LooseAssetError, WritebackAssetError, OSError, ValueError) as exc:
                item.state = "failed"
                item.message = f"Navmesh rebuild failed: {exc}"
                continue
            loose.source = item.relative
            key = (loose.container.lower(), loose.path_id)
        else:
            placeholders = Placeholders()
            try:
                source_path = substitute_name_references(item.path, staging, new_assets, placeholders, output_root, item.container)
                produced = build_writeback_asset(source_path, item.record, game_data)
            except (WritebackAssetError, LooseAssetError) as exc:
                item.state = "failed"
                item.message = str(exc).split(": ", 1)[-1] if str(exc).startswith(item.path.name) else str(exc)
                continue
            if produced is None:
                item.state = "failed"
                item.message = "Loose mods cant replace this kind of file yet, the AssetBundle Generator can bundle it"
                continue
            payload, object_name, class_id, details, resource_payload, resource_kind, script_ref = produced
            if resource_payload:
                item.state = "failed"
                item.message = f"Loose mods dont carry {resource_kind or 'resource'} data for this kind yet"
                continue
            fixes: list[tuple[int, str]] = []
            if placeholders.targets:
                seen: set[int] = set()
                payload, fixes = resolve_name_references(payload, placeholders, item.container, game_data, seen)
                dropped = dropped_references(placeholders, seen)
                if dropped:
                    item.state = "failed"
                    item.message = f"the reference to {dropped[0]} sits in a field that isnt an object pointer, so it was dropped"
                    continue
            loose = LooseObject(item.container, item.path_id, payload, class_id, object_name, fixes=fixes, source=item.relative, file_stem=Path(payload_name(item)).stem)
        targets[key] = item
        built.append((item, loose))
    if any(item.state == "failed" for item in sources):
        raise ModFolderError("some files couldnt be built, nothing was written")
    pieces = [loose for item, loose in built if loose is not None] + new_assets.objects
    if not pieces:
        raise ModFolderError("none of the files in this folder produced anything to load, so no mod was written")
    target, objects = write_loose_mod(game_data, mod_name, pieces)
    copy_mod_extras(folder, target)
    for item in sources:
        if item.state in ("matched", "new"):
            item.state = "built"
            item.message = summaries.get(id(item), "")
    if progress is not None:
        progress(1.0)
    return target, objects

def check_mods(data_root: Path, entries: list[ModEntry] | None = None) -> ModCheck:
    report = ModCheck()
    try:
        files = simulate(data_root, entries)
        unused_tables, report.overridden = active_overrides(data_root, entries)
    except (LooseFormatError, OSError, ValueError) as exc:
        report.problems.append(str(exc))
        return report
    for layout in files.values():
        report.applied.extend(item for item in layout.replaced + layout.added if item is not None)
        report.missing.extend(item for item in layout.missing if item is not None)
        report.problems.extend(layout.problems)
    return report

def mod_of(path: Path) -> str:
    parts = Path(path).parts
    for index, part in enumerate(parts[:-1]):
        if part.lower() == MODS_FOLDER.lower():
            return parts[index + 1]
    return ""

def remove_built_mod(game_data: Path, name: str) -> None:
    folder = mods_root(game_data) / safe_mod_name(name)
    entries = [entry for entry in read_mod_list(game_data) if entry.name.lower() != name.lower()]
    if folder.is_dir():
        shutil.rmtree(folder)
    write_mod_list(game_data, entries)

def export_mod_zip(folder: Path, target: Path) -> Path:
    import zipfile

    folder = Path(folder)
    if not folder.is_dir():
        raise ModFolderError(f"{folder.name} isnt built")
    target = Path(target)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                archive.write(path, (Path(folder.name) / path.relative_to(folder)).as_posix())
    return target

def import_mod_zip(archive_path: Path, game_data: Path) -> str:
    import zipfile

    with zipfile.ZipFile(archive_path) as archive:
        names = [name for name in archive.namelist() if not name.endswith("/")]
        if not names:
            raise ModFolderError("the zip is empty")
        tops = {name.replace("\\", "/").split("/", 1)[0] for name in names}
        if len(tops) != 1 or any("/" not in name.replace("\\", "/") for name in names):
            raise ModFolderError("a mod zip holds one folder named after the mod, as Zip in the Loose Builder makes it")
        top = tops.pop()
        for name in names:
            parts = name.replace("\\", "/").split("/")
            if any(part in ("", "..") or ":" in part for part in parts) or name.startswith("/"):
                raise ModFolderError(f"the zip has an unsafe path: {name}")
        if not any(name.lower().endswith((PAYLOAD_SUFFIX, "new_objects.txt", MOD_MANIFEST)) for name in names):
            raise ModFolderError("the zip doesnt hold a built Sunflowyr mod")
        mod_name = safe_mod_name(top)
        target = mods_root(game_data) / mod_name
        staging = mods_root(game_data) / f".{mod_name}.importing"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        for name in names:
            relative = Path(*name.replace("\\", "/").split("/")[1:])
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(name))
    if target.exists():
        shutil.rmtree(target)
    staging.rename(target)
    ensure_listed(game_data, mod_name)
    return mod_name

def game_folder(data_root: Path) -> Path:
    return Path(data_root).resolve().parent

def is_game_data(data_root: Path) -> bool:
    return (Path(data_root) / "globalgamemanagers").is_file() and (game_folder(data_root) / "rwby-ge.exe").is_file()

def is_sunflowyr_loader(path: Path) -> bool:
    try:
        return LOADER_MARKER in Path(path).read_bytes()
    except OSError:
        return False

def loader_state(data_root: Path) -> str:
    target = game_folder(data_root) / LOADER_NAME
    if not target.is_file():
        return "missing"
    if not is_sunflowyr_loader(target):
        return "other"
    try:
        current = LOADER_BUILD.is_file() and target.stat().st_size == LOADER_BUILD.stat().st_size and target.read_bytes() == LOADER_BUILD.read_bytes()
    except OSError:
        current = True
    return "installed" if current or not LOADER_BUILD.is_file() else "outdated"

def install_loader(data_root: Path, source: Path = LOADER_BUILD) -> list[str]:
    source = Path(source)
    if not source.is_file():
        raise ModFolderError(f"the loader file is missing at {source}, download Sunflowyr Engine again to get it back")
    folder = game_folder(data_root)
    if not (folder / "rwby-ge.exe").is_file():
        raise ModFolderError(f"{folder} doesnt hold rwby-ge.exe, pick the rwby-ge_Data folder of the game install")
    notes: list[str] = []
    target = folder / LOADER_NAME
    if target.is_file() and not is_sunflowyr_loader(target):
        parked = target.with_name(LOADER_NAME + OTHER_PROXY_SUFFIX)
        if parked.exists():
            raise ModFolderError(f"another version.dll is already parked at {parked.name}, move one of them out first")
        target.rename(parked)
        notes.append(f"Moved the other version.dll aside to {parked.name}")
    shutil.copy2(source, target)
    mods_root(data_root).mkdir(parents=True, exist_ok=True)
    notes.append("Installed the Sunflowyr loader")
    return notes

def uninstall_loader(data_root: Path) -> list[str]:
    folder = game_folder(data_root)
    target = folder / LOADER_NAME
    notes: list[str] = []
    if target.is_file() and is_sunflowyr_loader(target):
        target.unlink()
        notes.append("Removed the Sunflowyr loader")
    parked = folder / (LOADER_NAME + OTHER_PROXY_SUFFIX)
    if parked.is_file() and not target.exists():
        parked.rename(target)
        notes.append("Put the other version.dll back")
    return notes

def read_loader_log(data_root: Path) -> str:
    try:
        return (mods_root(data_root) / LOADER_LOG).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
