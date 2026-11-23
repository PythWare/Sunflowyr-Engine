from __future__ import annotations
import json, struct, subprocess, sys, zlib
from dataclasses import dataclass, field
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any
from .asset_bundle_audio import patch_audio_resource
from .asset_bundle_inputs import AssetBundleInputError, CollectedAssets, PreparedBundleAsset, PreparedGraph, collect_new_assets
from .asset_bundle_prefab import SENTINEL_BASE
from .asset_bundle_writer import MATERIAL_CLASS_ID, TYPE_HASHES, pack_aligned_string
from .binary import EndianReader
from .loose_format import SAME_FILE, LooseFormatError, LooseObject, container_folder
from .serialized import load_serialized_asset
from .taildata import BUILD_FOLDER

IDS_FILE = "sunflowyr_ids.json"
ID_FLOOR = 0x40000000
ID_SPAN = 0x10000
ID_SLOTS = 0x4000
SCENE_SPAN = 0x400
SCENE_SLOTS = 0x100
SCENE_LIMIT = 0x100000
DEFAULT_TARGET = "sharedassets0.assets"
GAME_SCRIPT_FILE = "globalgamemanagers.assets"
SCRIPT_POINTER_OFFSET = 16
SHADER_CLASS_ID = 48
REFERENCE_KEYS = ("file", "asset")
NAME_REFERENCE_BASE = SENTINEL_BASE + 0x8000_0000

class LooseAssetError(LooseFormatError):
    pass

def mod_id_base(name: str) -> int:
    return ID_FLOOR + (zlib.crc32(name.strip().lower().encode("utf-8")) % ID_SLOTS) * ID_SPAN

def is_scene_file(container: str | None) -> bool:
    name = Path(str(container or "")).name.lower()
    return name.startswith("level") and name[5:].isdigit()

@lru_cache(maxsize=16)
def highest_path_id(data_root: str, container: str) -> int:
    asset = load_serialized_asset(Path(data_root) / container, Path(data_root))
    return max((obj.path_id for obj in asset.objects), default=0)

def scene_id_base(name: str, data_root: Path, container: str) -> int:
    return highest_path_id(str(Path(data_root)), container) + 1 + (zlib.crc32(name.strip().lower().encode("utf-8")) % SCENE_SLOTS) * SCENE_SPAN

def normalized_assembly(name: str) -> str:
    name = str(name or "").strip()
    return (name[:-4] if name.lower().endswith(".dll") else name).lower()

@lru_cache(maxsize=4)
def game_scripts(data_root: str) -> dict[tuple[str, str, str], int]:
    from .mono_export import build_monoscript_index

    root = Path(data_root)
    path = root / GAME_SCRIPT_FILE
    if not path.is_file():
        return {}
    index = build_monoscript_index([load_serialized_asset(path, root)])
    return {(normalized_assembly(info.assembly_name), info.namespace or "", info.class_name): path_id for (unused, path_id), info in index.items()}

@lru_cache(maxsize=8)
def container_externals(data_root: str, container: str) -> tuple[str, ...]:
    root = Path(data_root)
    return tuple(load_serialized_asset(root / container_folder(container), root).externals)

def external_disk_path(data_root: Path, name: str) -> Path:
    normalized = name.replace("\\", "/")
    if normalized.lower().startswith("library/"):
        normalized = "Resources/" + normalized[len("library/"):]
    if normalized.lower().startswith("resources/"):
        normalized = "Resources/" + normalized[len("resources/"):]
    return Path(data_root) / normalized

@lru_cache(maxsize=4)
def game_shaders(data_root: str, container: str) -> dict[str, tuple[str, int]]:
    root = Path(data_root)
    found: dict[str, tuple[str, int]] = {}
    sources = [(SAME_FILE, root / container_folder(container))] + [(name, external_disk_path(root, name)) for name in container_externals(data_root, container)]
    for reference, path in sources:
        if not path.is_file():
            continue
        try:
            asset = load_serialized_asset(path, root)
        except Exception:
            continue
        with path.open("rb") as handle:
            for obj in asset.objects:
                if obj.class_id != SHADER_CLASS_ID:
                    continue
                handle.seek(obj.byte_start)
                data = handle.read(min(obj.byte_size, 262144))
                names = []
                length = struct.unpack_from("<i", data, 0)[0] if len(data) >= 4 else -1
                if 0 < length < 512:
                    names.append(data[4: 4 + length].decode("utf-8", "replace"))
                marker = data.find(b'Shader "')
                if marker >= 0:
                    stop = data.find(b'"', marker + 8)
                    if stop > marker:
                        names.append(data[marker + 8: stop].decode("utf-8", "replace"))
                for name in names:
                    found.setdefault(name.lower(), (reference, obj.path_id))
    return found

def audio_offset_field(payload: bytes) -> int:
    reader = EndianReader(BytesIO(payload), "<")
    reader.aligned_string(max_length=4096)
    for unused in range(4):
        reader.i32()
    reader.f32()
    reader.bool()
    reader.align()
    reader.i32()
    reader.bool()
    reader.bool()
    reader.bool()
    reader.align()
    reader.aligned_string(max_length=4096)
    return reader.tell()

def audio_host(data_root: Path, container: str) -> str:
    own = Path(container).with_suffix(".resource").as_posix()
    if (Path(data_root) / own).is_file():
        return own
    return "sharedassets0.resource"

class LooseIds:
    def __init__(self, folder: Path, mod_name: str, data_root: Path, container: str | None = None) -> None:
        self.folder = Path(folder)
        self.path = self.folder / IDS_FILE
        self.data_root = Path(data_root)
        self.mod_name = mod_name
        self.container = container if is_scene_file(container) else None
        document: dict[str, Any] = {}
        if self.path.is_file():
            try:
                document = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                document = {}
        self.base = int(document.get("base") or mod_id_base(mod_name))
        self.next = int(document.get("next") or self.base)
        self.blocks: dict[str, list[int]] = {key: list(value) for key, value in (document.get("blocks") or {}).items()}
        self.scenes: dict[str, list[int]] = {key: list(value) for key, value in (document.get("scenes") or {}).items()}
        self.used: set[str] = set()
        self.aliases: dict[str, Path] = {}
        self.scripts = game_scripts(str(self.data_root))

    def key(self, path: Path) -> str:
        path = self.aliases.get(str(Path(path).resolve()).lower(), path)
        try:
            return Path(path).resolve().relative_to(self.folder.resolve()).as_posix().lower()
        except ValueError:
            return Path(path).name.lower()

    def block(self, path: Path, count: int) -> int:
        if self.container is not None:
            return self.scene_block(path, count)
        key = self.key(path)
        self.used.add(key)
        current = self.blocks.get(key)
        if current is not None and current[1] >= count:
            return current[0]
        if self.next + count > self.base + ID_SPAN:
            raise LooseAssetError(f"this mod has more than {ID_SPAN} new objects, split it into two mods")
        self.blocks[key] = [self.next, count]
        self.next += count
        return self.blocks[key][0]

    def scene_block(self, path: Path, count: int) -> int:
        key = f"{self.container}|{self.key(path)}"
        self.used.add(key)
        current = self.blocks.get(key)
        if current is not None and current[1] >= count:
            return current[0]
        floor = highest_path_id(str(self.data_root), self.container) + 1
        base, following = self.scenes.get(self.container) or [scene_id_base(self.mod_name, self.data_root, self.container)] * 2
        if base < floor:
            base = following = scene_id_base(self.mod_name, self.data_root, self.container)
        if following + count > floor + SCENE_LIMIT:
            raise LooseAssetError(f"this mod adds more than {SCENE_LIMIT - SCENE_SLOTS * SCENE_SPAN} new objects to {self.container}, split it into two mods")
        self.blocks[key] = [following, count]
        self.scenes[self.container] = [base, following + count]
        return following

    def asset(self, item: PreparedBundleAsset) -> int:
        return self.block(item.input_path, 1)

    def graph(self, graph: PreparedGraph) -> int:
        return self.block(graph.input_path, len(graph.objects))

    def script(self, key: tuple[str, str, str], source: Path) -> int:
        found = self.scripts.get((normalized_assembly(key[0]), key[1] or "", key[2]))
        if found is None:
            raise AssetBundleInputError(f"{Path(source).name}: the script {'.'.join(part for part in key[1:] if part)} isnt one of the game scripts, so a loose mod cant use it")
        return found

    def save(self) -> None:
        document = {
            "format": "sunflowyr-loose-ids",
            "version": 1,
            "note": "Path IDs the Loose Builder gave this mods new files. Keep this file so rebuilds keep the same IDs.",
            "base": self.base,
            "next": self.next,
            "blocks": dict(sorted(self.blocks.items())),
            "scenes": dict(sorted(self.scenes.items())),
        }
        self.path.write_text(json.dumps(document, indent=2), encoding="utf-8")

@dataclass(slots=True)
class NewAssets:
    objects: list[LooseObject] = field(default_factory=list)
    by_file: dict[str, tuple[int, int, str, str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

def build_new_assets(files: list[Path], folder: Path, mod_name: str, data_root: Path, target: str = DEFAULT_TARGET, output_root: Path | None = None, build_root: Path | None = None, aliases: dict[str, Path] | None = None) -> NewAssets:
    data_root = Path(data_root)
    result = NewAssets()
    if not files:
        return result
    ids = LooseIds(folder, mod_name, data_root, target)
    ids.aliases.update(aliases or {})
    placeholders = Placeholders()
    staging = Path(build_root or Path(folder) / BUILD_FOLDER) / "game_references"
    inputs: list[Path] = []
    for path in files:
        wrapped = wrap_unpacked_copy(path, Path(build_root or Path(folder) / BUILD_FOLDER) / "copies")
        if wrapped != path:
            ids.aliases[str(Path(wrapped).resolve()).lower()] = ids.aliases.get(str(Path(path).resolve()).lower(), Path(path))
            path = wrapped
        try:
            if Path(path).is_file():
                copy = substitute_name_references(path, staging, None, placeholders, output_root, target, games_only=True)
            else:
                copy = substitute_in_folder(Path(path), staging, placeholders, output_root, target)
        except LooseAssetError as exc:
            raise LooseAssetError(f"{Path(path).name}: {exc}") from exc
        if copy != path:
            ids.aliases[str(Path(copy).resolve()).lower()] = ids.aliases.get(str(Path(path).resolve()).lower(), Path(path))
        inputs.append(copy)
    collected = collect_new_assets(inputs, data_root, ids)
    host = audio_host(data_root, target)
    prepared_by_id = {ids.asset(item): item for item in collected.prepared}
    graph_roots = {ids.graph(graph): graph for graph in collected.graphs}
    sources: dict[int, Path] = {path_id: item.input_path for path_id, item in prepared_by_id.items()}
    for graph in collected.graphs:
        start = ids.graph(graph)
        for offset in range(len(graph.objects)):
            sources[start + offset] = graph.input_path
    seen: set[int] = set()
    for obj in collected.objects:
        payload = obj.payload
        fixes: list[tuple[int, str]] = []
        resource = None
        resource_data = None
        if obj.script is not None:
            if payload[SCRIPT_POINTER_OFFSET: SCRIPT_POINTER_OFFSET + 4] != b"\0\0\0\0" or struct.unpack_from("<q", payload, SCRIPT_POINTER_OFFSET + 4)[0] != obj.script.path_id:
                raise LooseAssetError(f"{obj.name}: its script pointer isnt where the loose builder expects it")
            fixes.append((SCRIPT_POINTER_OFFSET, GAME_SCRIPT_FILE))
            add = ("script", 0, obj.script.path_id, obj.script.properties_hash(), obj.script.type_hash(), GAME_SCRIPT_FILE)
        else:
            if obj.class_id not in TYPE_HASHES:
                raise LooseAssetError(f"{obj.name}: class {obj.class_id} has no RWBY GE type definition")
            add = ("class", obj.class_id, 0, TYPE_HASHES[obj.class_id])
        if obj.path_id in collected.resources:
            resource_data = collected.resources[obj.path_id]
            payload = patch_audio_resource(payload, host, 0, len(resource_data))
            resource = (audio_offset_field(payload), host)
        prepared = prepared_by_id.get(obj.path_id)
        if obj.class_id == MATERIAL_CLASS_ID and prepared is not None:
            payload, fixes = attach_shader(payload, obj.name, prepared, data_root, target, fixes, result.notes)
        source = sources.get(obj.path_id)
        relative = ids.key(source) if source is not None else ""
        if placeholders.targets:
            payload, extra = resolve_name_references(payload, placeholders, target, data_root, seen)
            fixes = fixes + extra
        result.objects.append(LooseObject(target, obj.path_id, payload, obj.class_id, obj.name, add=add, fixes=fixes, resource=resource, resource_data=resource_data, source=relative))
    dropped = dropped_references(placeholders, seen)
    if dropped:
        raise LooseAssetError(f"the reference to {dropped[0]} sits in a field that isnt an object pointer, so it was dropped")
    for path_id, item in prepared_by_id.items():
        result.by_file[item.input_path.name.lower()] = (path_id, item.class_id, item.name, target)
    for path_id, graph in graph_roots.items():
        result.by_file[graph.input_path.name.lower()] = (path_id, graph.objects[0].class_id, graph.name, target)
    ids.save()
    return result

UNPACKED_HEADER = ("m_GameObject", "m_Enabled", "m_Script", "m_Name", "solarScript")

def wrap_unpacked_copy(path: Path, staging: Path) -> Path:
    from .asset_bundle_prefab import MONO_ASSET_FORMAT

    document = read_json_document(path)
    if not isinstance(document, dict) or "format" in document:
        return path
    script = document.get("solarScript")
    if not isinstance(script, dict) or not isinstance(document.get("m_Script"), dict):
        return path
    assembly = str(script.get("assembly_name") or "Assembly-CSharp")
    if assembly.lower().endswith(".dll"):
        assembly = assembly[:-4]
    wrapped = {
        "format": MONO_ASSET_FORMAT,
        "name": Path(path).stem,
        "script": {"assembly": assembly, "namespace": str(script.get("namespace") or ""), "class": str(script.get("class_name") or script.get("name") or "")},
        "fields": {key: value for key, value in document.items() if key not in UNPACKED_HEADER},
    }
    staging.mkdir(parents=True, exist_ok=True)
    copy = staging / Path(path).name
    copy.write_text(json.dumps(wrapped, ensure_ascii=False, indent=2), encoding="utf-8")
    return copy

def attach_shader(payload: bytes, name: str, prepared: PreparedBundleAsset, data_root: Path, target: str, fixes: list[tuple[int, str]], notes: list[str]) -> tuple[bytes, list[tuple[int, str]]]:
    offset = len(pack_aligned_string(name))
    if payload[offset: offset + 12] != b"\0" * 12:
        return payload, fixes
    shader_name = str(prepared.import_details.get("shaderName") or "")
    if not shader_name:
        return payload, fixes
    found = game_shaders(str(data_root), target).get(shader_name.lower())
    if found is None:
        notes.append(f"{prepared.input_path.name}: no game shader is named {shader_name}, the material has no shader")
        return payload, fixes
    reference, path_id = found
    data = bytearray(payload)
    struct.pack_into("<iq", data, offset, 0, path_id)
    if reference != SAME_FILE:
        fixes = fixes + [(offset, reference)]
    return bytes(data), fixes

GAME_KEY = "game"
GAME_FILTERS = {"game", "type", "in", "id"}
FINDER_INDEX = "sunflowyr_finder_index.tsv"
RAW_SECTION = "\x1eraw"

@dataclass(slots=True)
class Placeholders:
    next_index: int = 0
    targets: dict[int, tuple[int, str, str]] = field(default_factory=dict)

    def take(self, path_id: int, container: str, label: str) -> int:
        sentinel = NAME_REFERENCE_BASE + self.next_index
        self.next_index += 1
        self.targets[sentinel] = (path_id, container, label)
        return sentinel

def is_new_file_reference(value: dict) -> bool:
    keys = [key for key in REFERENCE_KEYS if key in value]
    return bool(keys) and len(value) == 1 and isinstance(value[keys[0]], str)

def is_game_reference(value: dict) -> bool:
    return isinstance(value.get(GAME_KEY), str) and set(value) <= GAME_FILTERS

def find_name_references(value: Any, found: list[dict], games_only: bool = False) -> None:
    if isinstance(value, dict):
        if is_game_reference(value) or (not games_only and is_new_file_reference(value)):
            found.append(value)
            return
        for item in value.values():
            find_name_references(item, found, games_only)
    elif isinstance(value, list):
        for item in value:
            find_name_references(item, found, games_only)

def read_json_document(path: Path) -> Any | None:
    if Path(path).suffix.lower() != ".json":
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None

@lru_cache(maxsize=2)
def game_names(index_path: str, stamp: int) -> dict[str, list[tuple[str, int, int, str]]]:
    from .class_ids import CLASS_ID_NAMES

    names: dict[str, list[tuple[str, int, int, str]]] = {}
    text = Path(index_path).read_text(encoding="utf-8")
    start = text.find(RAW_SECTION)
    if start < 0:
        return names
    end = text.find("\x1e", start + 1)
    for line in text[start + len(RAW_SECTION): end if end > 0 else len(text)].splitlines():
        fields = line.split("\t")
        relative, container = fields[0], (fields[1] if len(fields) > 1 else "")
        parts = relative.split("/")
        if len(parts) < 3 or "__path" not in parts[-1]:
            continue
        folder = parts[-2]
        stem = parts[-1].rsplit(".", 1)[0]
        name, unused, digits = stem.rpartition("__path")
        try:
            class_id = int(folder.split("_", 1)[0])
            path_id = int(digits)
        except ValueError:
            continue
        class_label = folder.split("_", 1)[1] if "_" in folder else str(CLASS_ID_NAMES.get(class_id, class_id))
        names.setdefault(name.lower(), []).append((container, path_id, class_id, class_label))
    return names

def game_index(output_root: Path | None) -> dict[str, list[tuple[str, int, int, str]]]:
    path = Path(output_root) / FINDER_INDEX if output_root else None
    if path is None or not path.is_file():
        raise LooseAssetError("game references need the unpack asset index, open the Asset Finder once so it gets built")
    return game_names(str(path), int(path.stat().st_mtime))

def resolve_game_reference(spec: dict, output_root: Path | None, prefer: str) -> tuple[int, str, str]:
    name = str(spec[GAME_KEY])
    if spec.get("id") is not None and spec.get("in"):
        return int(spec["id"]), str(spec["in"]), f'game object "{name}" ({spec["id"]} in {spec["in"]})'
    candidates = game_index(output_root).get(name.lower(), [])
    wanted_type = str(spec.get("type") or "").lower()
    wanted_file = str(spec.get("in") or "").replace("\\", "/").lower()
    wanted_id = spec.get("id")
    matches = [item for item in candidates
               if (not wanted_type or item[3].lower() == wanted_type or str(item[2]) == wanted_type)
               and (not wanted_file or item[0].replace("\\", "/").lower() == wanted_file)
               and (wanted_id is None or item[1] == int(wanted_id))]
    if len(matches) > 1:
        local = [item for item in matches if item[0].replace("\\", "/").lower() == prefer.replace("\\", "/").lower()]
        matches = local or matches
    label = f'game object "{name}"'
    if not matches:
        raise LooseAssetError(f"no {label} matches" + (f' ("type": "{spec.get("type")}")' if wanted_type else "") + (f' in {spec.get("in")}' if wanted_file else ""))
    if len(matches) > 1:
        listed = ", ".join(f"{item[3]} {item[1]} in {item[0]}" for item in matches[:6])
        raise LooseAssetError(f'{label} matches {len(matches)} objects ({listed}), add "type", "in" or "id" to pick one')
    container, path_id, unused, class_label = matches[0]
    return path_id, container, f"{label} ({class_label} {path_id})"

def substitute_in_folder(folder: Path, staging: Path, placeholders: Placeholders, output_root: Path | None, own_container: str) -> Path:
    import shutil

    changed: dict[Path, Any] = {}
    for path in sorted(folder.glob("*.json")):
        document = read_json_document(path)
        if document is None:
            continue
        try:
            if substitute_in_value(document, None, placeholders, output_root, own_container, games_only=True):
                changed[path] = document
        except LooseAssetError as exc:
            raise LooseAssetError(f"{path.name}: {exc}") from exc
    if not changed:
        return folder
    target = staging / folder.name
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for path in folder.iterdir():
        if path.is_file():
            if path in changed:
                (target / path.name).write_text(json.dumps(changed[path], ensure_ascii=False, indent=2), encoding="utf-8")
            else:
                shutil.copy2(path, target / path.name)
    return target

def substitute_name_references(path: Path, staging: Path, new_assets: NewAssets | None, placeholders: Placeholders, output_root: Path | None, own_container: str, games_only: bool = False) -> Path:
    document = read_json_document(path)
    if document is None:
        return path
    if not substitute_in_value(document, new_assets, placeholders, output_root, own_container, games_only):
        return path
    staging.mkdir(parents=True, exist_ok=True)
    copy = staging / Path(path).name
    copy.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return copy

def substitute_in_value(document: Any, new_assets: NewAssets | None, placeholders: Placeholders, output_root: Path | None, own_container: str, games_only: bool = False) -> bool:
    found: list[dict] = []
    find_name_references(document, found, games_only)
    for holder in found:
        if is_game_reference(holder):
            path_id, container, label = resolve_game_reference(holder, output_root, own_container)
        else:
            name = holder.get("file") or holder.get("asset")
            target = (new_assets.by_file if new_assets else {}).get(Path(str(name).replace("\\", "/")).name.lower())
            if target is None:
                raise LooseAssetError(f'it points at "{name}", which isnt a new file in this mod folder')
            path_id, container, label = target[0], target[3], f'"{name}"'
        holder.clear()
        holder["m_FileID"] = 0
        holder["m_PathID"] = placeholders.take(path_id, container, label)
    return bool(found)

def managed_registry(data_root: str) -> Any:
    from .managed_schema import shared_registry

    return shared_registry(Path(data_root) / "Managed")

@lru_cache(maxsize=2)
def script_index(data_root: str) -> dict:
    from .mono_export import build_monoscript_index

    root = Path(data_root)
    return build_monoscript_index([load_serialized_asset(root / GAME_SCRIPT_FILE, root)])

def game_mono_document(data_root: Path, container: str, path_id: int) -> tuple[dict, bytes]:
    from .mono_export import read_monobehaviour_json

    root = Path(data_root)
    asset = load_serialized_asset(root / container_folder(container), root)
    obj = next((item for item in asset.objects if item.path_id == path_id), None)
    if obj is None:
        raise LooseAssetError(f"{container} has no object {path_id}")
    with asset.path.open("rb") as handle:
        document, unused = read_monobehaviour_json(asset, EndianReader(handle, asset.endian), obj, script_index(str(root)), managed_registry(str(root)))
        handle.seek(obj.byte_start)
        raw = handle.read(obj.byte_size)
    return document, raw

def repack_document(document: dict, record: dict[str, Any], data_root: Path, scratch: Path, name: str) -> bytes:
    from .asset_bundle_writeback import build_writeback_asset

    scratch.mkdir(parents=True, exist_ok=True)
    path = scratch / name
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    produced = build_writeback_asset(path, record, Path(data_root))
    if produced is None:
        raise LooseAssetError("this object cant be rebuilt")
    return produced[0]

def build_list_additions(path: Path, record: dict[str, Any], data_root: Path, new_assets: NewAssets | None, output_root: Path | None, scratch: Path) -> list[Any]:
    import copy as copying
    from .loose_format import ListAddition

    if int(record.get("classId") or 0) in TABLE_LIST_FIELDS:
        return build_table_additions(path, record, data_root, new_assets, output_root)
    if str(record.get("kind") or "") != "monobehaviour_json":
        raise LooseAssetError("list additions work on MonoBehaviour lists, the ResourceManager and PreloadData")
    wanted = read_json_document(path)
    if not isinstance(wanted, dict) or not wanted:
        raise LooseAssetError('an .add.json holds {"list name": [entries to add]}')
    container = str(record.get("container") or "")
    path_id = int(record.get("pathId") or 0)
    original, raw = game_mono_document(data_root, container, path_id)
    base = repack_document(original, record, data_root, scratch, "base.json")
    if base != raw:
        raise LooseAssetError("this object doesnt rebuild back identically, so entries cant be added to its lists safely")
    additions = []
    for field_name, entries in wanted.items():
        current = original.get(field_name)
        if not isinstance(current, list):
            raise LooseAssetError(f'"{field_name}" isnt a list in this object')
        if not isinstance(entries, list) or not entries:
            raise LooseAssetError(f'"{field_name}" needs a list of entries to add')
        placeholders = Placeholders()
        added = copying.deepcopy(entries)
        substitute_in_value(added, new_assets, placeholders, output_root, container)
        grown = copying.deepcopy(original)
        grown[field_name] = list(current) + added
        emptied = copying.deepcopy(original)
        emptied[field_name] = []
        larger = repack_document(grown, record, data_root, scratch, "grown.json")
        smaller = repack_document(emptied, record, data_root, scratch, "emptied.json")
        count_offset, insert_offset, blob = locate_addition(base, larger, smaller, len(current), len(added), field_name)
        seen: set[int] = set()
        blob, fixes = resolve_name_references(blob, placeholders, container, data_root, seen)
        dropped = dropped_references(placeholders, seen)
        if dropped:
            raise LooseAssetError(f"the reference to {dropped[0]} sits in a field that isnt an object pointer, so it was dropped")
        additions.append(ListAddition(count_offset, insert_offset, len(added), blob, fixes, field_name, len(raw)))
    return additions

TABLE_LIST_FIELDS = {
    147: {"m_Container": "m_Container", "container": "m_Container", "m_DependentAssets": "m_DependentAssets", "dependent_assets": "m_DependentAssets"},
    150: {"m_Assets": "m_Assets", "assets": "m_Assets", "m_Dependencies": "m_Dependencies", "dependencies": "m_Dependencies"},
}

def table_reference(value: Any, where: str) -> Any:
    if isinstance(value, dict) and (is_game_reference(value) or is_new_file_reference(value)):
        return dict(value)
    if isinstance(value, dict) and ("m_PathID" in value or "pathId" in value):
        return {"m_FileID": int(value.get("m_FileID", value.get("fileId", 0)) or 0), "m_PathID": int(value.get("m_PathID", value.get("pathId", 0)) or 0)}
    raise LooseAssetError(f'{where} needs an object, like {{"file": "Name.prefab.json"}} or {{"game": "Name"}}')

def table_entry(field_name: str, entry: Any) -> Any:
    if field_name == "m_Container":
        if isinstance(entry, list) and len(entry) == 2:
            key, target = entry
        elif isinstance(entry, dict):
            key, target = entry.get("path", entry.get("asset_key")), entry.get("object", entry.get("asset"))
        else:
            raise LooseAssetError('a ResourceManager container entry is ["resources path", {"file": "Name"}] or {"path": ..., "object": ...}')
        if not isinstance(key, str) or not key.strip():
            raise LooseAssetError("a ResourceManager container entry needs the path Resources.Load uses")
        return [key.strip().replace("\\", "/").lower(), table_reference(target, f'"{key}"')]
    if field_name == "m_DependentAssets":
        if not isinstance(entry, dict):
            raise LooseAssetError('a dependent asset entry is {"object": {...}, "dependencies": [{...}]}')
        target = entry.get("object", entry.get("m_Object"))
        needs = entry.get("dependencies", entry.get("m_Dependencies")) or []
        if not isinstance(needs, list):
            raise LooseAssetError('"dependencies" needs a list of objects')
        return {"m_Object": table_reference(target, "a dependent asset"), "m_Dependencies": [table_reference(item, "a dependency") for item in needs]}
    if field_name == "m_Assets":
        return table_reference(entry, "a preload entry")
    if not isinstance(entry, str) or not entry:
        raise LooseAssetError("PreloadData dependencies are file names")
    return entry

def build_table_additions(path: Path, record: dict[str, Any], data_root: Path, new_assets: NewAssets | None, output_root: Path | None) -> list[Any]:
    import copy as copying
    from .editable_object_export import EDITABLE_ASSET_SCHEMAS
    from .loose_format import ListAddition
    from .unity_typetree_codec import load_schema, read_typetree, write_typetree

    class_id = int(record.get("classId") or 0)
    aliases = TABLE_LIST_FIELDS[class_id]
    wanted = read_json_document(path)
    if not isinstance(wanted, dict) or not wanted:
        raise LooseAssetError('an .add.json holds {"list name": [entries to add]}, for example {"container": [["path", {"file": "Name.prefab.json"}]]}')
    container = str(record.get("container") or "")
    path_id = int(record.get("pathId") or 0)
    root = Path(data_root)
    asset = load_serialized_asset(root / container_folder(container), root)
    obj = next((item for item in asset.objects if item.path_id == path_id), None)
    if obj is None:
        raise LooseAssetError(f"{container} has no object {path_id}")
    with asset.path.open("rb") as handle:
        handle.seek(obj.byte_start)
        raw = handle.read(obj.byte_size)
    schema = load_schema(EDITABLE_ASSET_SCHEMAS[class_id][1])
    original = read_typetree(raw, schema, asset.endian)
    base = write_typetree(original, schema, asset.endian)
    if base != raw:
        raise LooseAssetError("this object doesnt rebuild back identically, so entries cant be added to its lists safely")
    additions = []
    for name, entries in wanted.items():
        field_name = aliases.get(name)
        if field_name is None:
            raise LooseAssetError(f'"{name}" isnt a list this object takes additions for, use one of {", ".join(sorted(set(aliases)))}')
        if not isinstance(entries, list) or not entries:
            raise LooseAssetError(f'"{name}" needs a list of entries to add')
        current = list(original.get(field_name) or [])
        added = [table_entry(field_name, entry) for entry in entries]
        placeholders = Placeholders()
        substitute_in_value(added, new_assets, placeholders, output_root, container)
        grown = copying.deepcopy(original)
        grown[field_name] = current + copying.deepcopy(added)
        emptied = copying.deepcopy(original)
        emptied[field_name] = []
        larger = write_typetree(grown, schema, asset.endian)
        smaller = write_typetree(emptied, schema, asset.endian)
        count_offset, insert_offset, blob = locate_addition(base, larger, smaller, len(current), len(added), field_name)
        seen: set[int] = set()
        blob, fixes = resolve_name_references(blob, placeholders, container, root, seen)
        dropped = dropped_references(placeholders, seen)
        if dropped:
            raise LooseAssetError(f"the reference to {dropped[0]} sits in a field that isnt an object pointer, so it was dropped")
        additions.append(ListAddition(count_offset, insert_offset, len(added), blob, fixes, field_name, len(raw)))
    return additions

def locate_addition(base: bytes, larger: bytes, smaller: bytes, count: int, added: int, field_name: str) -> tuple[int, int, bytes]:
    grown = len(larger) - len(base)
    list_bytes = len(base) - len(smaller)
    first = next((index for index in range(len(base)) if base[index] != larger[index]), len(base))
    count_offset = next((offset for offset in range(max(0, first - 3), first + 1)
                         if offset + 4 <= len(base) and struct.unpack_from("<i", base, offset)[0] == count and struct.unpack_from("<i", larger, offset)[0] == count + added), None)
    if count_offset is None or grown <= 0 or list_bytes < 0:
        raise LooseAssetError(f'couldnt find where "{field_name}" is stored in this object')
    insert = count_offset + 4 + list_bytes
    if larger[:count_offset] != base[:count_offset] or larger[count_offset + 4: insert] != base[count_offset + 4: insert] or larger[insert + grown:] != base[insert:]:
        raise LooseAssetError(f'"{field_name}" isnt stored as one plain list, so entries cant be added to it')
    return count_offset, insert, larger[insert: insert + grown]

def external_name(data_root: Path, own_container: str, container: str) -> str:
    wanted = container.replace("\\", "/").lower()
    for name in container_externals(str(data_root), own_container):
        if external_disk_path(Path(data_root), name).resolve() == (Path(data_root) / container).resolve():
            return name
    return container.replace("\\", "/")

def resolve_name_references(payload: bytes, placeholders: Placeholders, own_container: str, data_root: Path, seen: set[int] | None = None) -> tuple[bytes, list[tuple[int, str]]]:
    data = bytearray(payload)
    fixes: list[tuple[int, str]] = []
    for sentinel, (path_id, container, label) in placeholders.targets.items():
        needle = struct.pack("<q", sentinel)
        position = data.find(needle)
        if position >= 4 and seen is not None:
            seen.add(sentinel)
        while position >= 4:
            struct.pack_into("<q", data, position, path_id)
            struct.pack_into("<i", data, position - 4, 0)
            if own_container.replace("\\", "/").lower() != container.replace("\\", "/").lower():
                fixes.append((position - 4, external_name(data_root, own_container, container)))
            position = data.find(needle, position + 8)
    return bytes(data), fixes

def dropped_references(placeholders: Placeholders, seen: set[int]) -> list[str]:
    return [label for sentinel, (unused, unused_container, label) in placeholders.targets.items() if sentinel not in seen]

HIERARCHY_KINDS = ("reconstructed_prefab_glb", "reconstructed_scene_glb")

def build_navmesh_source(path: Path, record: dict[str, Any], folder: Path, data_root: Path) -> LooseObject:
    from .asset_bundle_writeback import build_writeback_asset
    from .navmesh_from_glb import NavMeshFromGlbError, rebuild_navmesh_folder

    out = Path(folder) / BUILD_FOLDER / "navmesh" / Path(path).stem
    try:
        result = rebuild_navmesh_folder(Path(path), out, Path(data_root))
    except NavMeshFromGlbError as exc:
        raise LooseAssetError(str(exc)) from exc
    json_path = Path(result["jsonPath"])
    converted = json.loads((out / "sunflowyr_taildata.json").read_text(encoding="utf-8"))["files"][json_path.name]
    produced = build_writeback_asset(json_path, converted, Path(data_root))
    if produced is None:
        raise LooseAssetError("the converted navmesh couldnt be rebuilt")
    payload, name, class_id = produced[0], produced[1], produced[2]
    return LooseObject(str(result["container"]), int(result["pathId"]), payload, class_id, name, file_stem=f"{name}__path{int(result['pathId'])}")

HIERARCHY_MARKERS = (b"unity_transform_path_id", b"unity_game_object_path_id")

def glb_json(path: Path) -> dict[str, Any] | None:
    try:
        with Path(path).open("rb") as handle:
            header = handle.read(20)
            if len(header) < 20 or header[:4] != b"glTF":
                return None
            length, kind = struct.unpack_from("<I4s", header, 12)
            if kind != b"JSON":
                return None
            return json.loads(handle.read(length))
    except (OSError, ValueError):
        return None

def scene_glb_container(path: Path) -> str | None:
    if Path(path).suffix.lower() != ".glb":
        return None
    document = glb_json(path)
    if not document:
        return None
    containers = {str(node["extras"].get("unity_container") or "") for node in document.get("nodes") or []
                  if isinstance(node.get("extras"), dict) and node["extras"].get("unity_transform_path_id") is not None}
    if len(containers) != 1:
        return None
    container = containers.pop()
    return container if container.lower().startswith("level") and container[5:].isdigit() else None

def is_hierarchy_glb_file(path: Path) -> bool:
    if Path(path).suffix.lower() != ".glb":
        return False
    try:
        with Path(path).open("rb") as handle:
            chunk = handle.read(4 * 1024 * 1024)
    except OSError:
        return False
    return any(marker in chunk for marker in HIERARCHY_MARKERS)

CONVERSION_VERSION = "game-refs-1"

def convert_hierarchy_glbs(files: list[Path], folder: Path, data_root: Path, cache: Path | None = None) -> tuple[list[Path], list[str]]:
    converted: list[Path] = []
    problems: list[str] = []
    cache = Path(cache or Path(folder) / BUILD_FOLDER)
    for path in files:
        out = cache / Path(path).stem
        stamp = out / ".source_stamp"
        signature = f"{CONVERSION_VERSION}:{Path(path).stat().st_size}:{Path(path).stat().st_mtime_ns}"
        if not (stamp.is_file() and stamp.read_text(encoding="utf-8") == signature):
            if out.exists():
                import shutil

                shutil.rmtree(out)
            command = [sys.executable, str(Path(__file__).resolve().parent.parent / "solar_cli.py"), "prefab-from-glb", str(path), "--out", str(out), "--data", str(data_root), "--game-refs"]
            result = subprocess.run(command, capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent.parent))
            if result.returncode != 0:
                tail = (result.stdout + result.stderr).strip().splitlines()[-1:] or ["conversion failed"]
                problems.append(f"{Path(path).name}: {tail[0]}")
                continue
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.write_text(signature, encoding="utf-8")
        converted.append(out)
    return converted, problems
