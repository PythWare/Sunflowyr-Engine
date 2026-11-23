from __future__ import annotations
import json, os, re, shutil, struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from .class_ids import class_name

MODS_FOLDER = "Sunflowyr_Mods"
MOD_LIST = "mods.txt"
MOD_MANIFEST = "sunflowyr_mod.json"
DESCRIPTOR = "new_objects.txt"
PAYLOAD_SUFFIX = ".bin"
RESOURCE_SUFFIX = ".resource"
OBJECT_ALIGNMENT = 8
RESOURCE_ALIGNMENT = 32
SIZE_LIMIT = 0xFFFFFFFF
PAYLOAD_NAME = re.compile(r"^(?:.*__path)?(-?\d+)\.bin$", re.IGNORECASE)
UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
SAME_FILE = "."

class LooseFormatError(ValueError):
    pass

@dataclass(slots=True)
class ListAddition:
    count_offset: int
    insert_offset: int
    count_add: int
    blob: bytes
    fixes: list[tuple[int, str]] = field(default_factory=list)
    field_name: str = ""
    base_size: int = 0

@dataclass(slots=True)
class AppendSpec:
    container: str
    path_id: int
    number: int
    count_offset: int
    insert_offset: int
    count_add: int
    blob: Path
    fixes: list[tuple[int, str]] = field(default_factory=list)
    base_size: int = 0

@dataclass(slots=True)
class LooseObject:
    container: str
    path_id: int
    payload: bytes | None
    class_id: int = 0
    name: str = ""
    add: tuple | None = None
    fixes: list[tuple[int, str]] = field(default_factory=list)
    resource: tuple[int, str] | None = None
    resource_data: bytes | None = None
    source: str = ""
    file_stem: str = ""
    additions: list[ListAddition] = field(default_factory=list)

@dataclass(slots=True)
class ModObject:
    container: str
    path_id: int
    class_id: int
    name: str
    payload: Path
    size: int
    source: str = ""
    add: tuple | None = None
    fixes: list[tuple[int, str]] = field(default_factory=list)
    resource: tuple[int, str] | None = None
    described: bool = False

    @property
    def class_label(self) -> str:
        return class_name(self.class_id) if self.class_id else "Object"

    @property
    def is_new(self) -> bool:
        return self.add is not None

@dataclass(slots=True)
class ModEntry:
    name: str
    enabled: bool
    folder: Path
    objects: list[ModObject] = field(default_factory=list)
    appends: list[AppendSpec] = field(default_factory=list)

MOD_INFO = "mod_info.txt"
PREVIEW_FOLDER = "preview"
PREVIEW_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tga")
INFO_KEYS = ("author", "version")

@dataclass(slots=True)
class ModInfo:
    author: str = ""
    version: str = ""
    description: str = ""

def read_mod_info(folder: Path) -> ModInfo:
    info = ModInfo()
    try:
        text = (Path(folder) / MOD_INFO).read_text(encoding="utf-8-sig")
    except OSError:
        return info
    lines = text.splitlines()
    body_start = len(lines)
    for index, line in enumerate(lines):
        if not line.strip():
            body_start = index + 1
            break
        key, separator, value = line.partition(":")
        if separator and key.strip().lower() in INFO_KEYS:
            setattr(info, key.strip().lower(), value.strip())
        else:
            body_start = index
            break
    info.description = "\n".join(lines[body_start:]).strip()
    return info

def write_mod_info(folder: Path, info: ModInfo) -> Path:
    target = Path(folder) / MOD_INFO
    text = f"Author: {info.author.strip()}\nVersion: {info.version.strip()}\n\n{info.description.strip()}\n"
    target.write_text(text, encoding="utf-8")
    return target

def preview_images(folder: Path) -> list[Path]:
    preview = Path(folder) / PREVIEW_FOLDER
    if not preview.is_dir():
        return []
    return sorted((path for path in preview.iterdir() if path.is_file() and path.suffix.lower() in PREVIEW_SUFFIXES), key=lambda path: path.name.lower())

def copy_mod_extras(source: Path, target: Path) -> None:
    info = Path(source) / MOD_INFO
    if info.is_file():
        shutil.copy2(info, Path(target) / MOD_INFO)
    images = preview_images(source)
    if images:
        destination = Path(target) / PREVIEW_FOLDER
        destination.mkdir(parents=True, exist_ok=True)
        for image in images:
            shutil.copy2(image, destination / image.name)

def mods_root(data_root: Path) -> Path:
    return Path(data_root) / MODS_FOLDER

def safe_mod_name(name: str) -> str:
    cleaned = UNSAFE_NAME.sub("", name.strip()).strip(". ")
    if not cleaned:
        raise LooseFormatError("a mod needs a name made of letters, numbers or spaces")
    return cleaned

def container_folder(container: str) -> Path:
    parts = [part for part in container.replace("\\", "/").split("/") if part]
    if not parts or any(part in (".", "..") for part in parts):
        raise LooseFormatError(f"{container} isnt a game file name")
    return Path(*parts)

def payload_path_id(file_name: str) -> int | None:
    match = PAYLOAD_NAME.match(file_name)
    return int(match.group(1)) if match else None

def loose_file_stem(item: LooseObject) -> str:
    stem = item.file_stem or f"{item.name or class_name(item.class_id) or 'object'}__path{item.path_id}"
    return UNSAFE_NAME.sub("_", stem).strip(". ")[:150] or f"object__path{item.path_id}"

def descriptor_lines(item: LooseObject | ModObject) -> list[str]:
    lines: list[str] = []
    add = item.add
    if add is not None:
        if add[0] == "raw":
            lines.append(f"{item.path_id} {add[1]} {add[2]} {add[3]} {add[4]}")
        elif add[0] == "class":
            lines.append(f"new {item.path_id} {add[1]} {add[2]} {add[3].hex()}")
        elif add[0] == "script":
            lines.append(f"script {item.path_id} {add[1]} {add[2]} {add[3].hex()} {add[4].hex()} {add[5] or SAME_FILE}")
        else:
            raise LooseFormatError(f"unknown new object kind {add[0]}")
    for offset, target in item.fixes:
        lines.append(f"fix {item.path_id} {offset} {target or SAME_FILE}")
    if item.resource is not None:
        lines.append(f"resource {item.path_id} {item.resource[0]} {item.resource[1]}")
    return lines

WRITE_THREADS = 8
THREADED_WRITES = 64

def write_all(writes: list[tuple[Path, bytes]]) -> None:
    if len(writes) < THREADED_WRITES:
        for path, data in writes:
            path.write_bytes(data)
        return
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(WRITE_THREADS) as pool:
        list(pool.map(lambda job: job[0].write_bytes(job[1]), writes))

def write_loose_mod(game_data: Path, name: str, items: list[LooseObject], *, enable: bool = True) -> tuple[Path, list[ModObject]]:
    game_data = Path(game_data)
    mod_name = safe_mod_name(name)
    root = mods_root(game_data)
    target = root / mod_name
    staging = root / f".{mod_name}.building"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    seen: set[tuple[str, int]] = set()
    objects: list[ModObject] = []
    additions: list[dict[str, Any]] = []
    descriptors: dict[str, list[str]] = {}
    made: set[Path] = set()
    writes: list[tuple[Path, bytes]] = []
    try:
        for item in sorted(items, key=lambda value: (value.container.lower(), value.path_id)):
            key = (item.container.lower(), item.path_id)
            if key in seen:
                raise LooseFormatError(f"{item.container} path {item.path_id} is written twice in {mod_name}")
            seen.add(key)
            if (item.resource is None) != (item.resource_data is None):
                raise LooseFormatError(f"{item.container} path {item.path_id} needs both a resource offset and resource data")
            stem = loose_file_stem(item)
            folder = staging / container_folder(item.container)
            if folder not in made:
                folder.mkdir(parents=True, exist_ok=True)
                made.add(folder)
            if item.payload is not None:
                relative = container_folder(item.container) / (stem + PAYLOAD_SUFFIX)
                destination = staging / relative
                writes.append((destination, item.payload))
                if item.resource_data is not None:
                    writes.append((destination.with_suffix(RESOURCE_SUFFIX), item.resource_data))
                lines = descriptor_lines(item)
                if lines:
                    descriptors.setdefault(item.container, []).extend(lines)
                objects.append(ModObject(item.container, item.path_id, item.class_id, item.name, target / relative, len(item.payload), item.source, item.add, list(item.fixes), item.resource, bool(lines)))
            for number, addition in enumerate(item.additions, start=1):
                blob_name = f"{stem}.add{number}{PAYLOAD_SUFFIX}"
                writes.append((folder / blob_name, addition.blob))
                lines = descriptors.setdefault(item.container, [])
                lines.append(f"append {item.path_id} {number} {addition.count_offset} {addition.insert_offset} {addition.count_add} {addition.base_size} {blob_name}")
                lines.extend(f"appendfix {item.path_id} {number} {offset} {external}" for offset, external in addition.fixes)
                additions.append({"file": f"{container_folder(item.container).as_posix()}/{blob_name}", "container": item.container, "pathId": item.path_id,
                                  "name": item.name, "list": addition.field_name, "entries": addition.count_add, "source": item.source})
        write_all(writes)
        for container, lines in descriptors.items():
            (staging / container_folder(container) / DESCRIPTOR).write_text("\n".join(lines) + "\n", encoding="utf-8")
        write_manifest(staging, mod_name, objects, target, additions)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if target.exists():
        shutil.rmtree(target)
    os.replace(staging, target)
    if enable:
        ensure_listed(game_data, mod_name)
    return target, objects

def write_manifest(folder: Path, name: str, objects: list[ModObject], final: Path, additions: list[dict[str, Any]] | None = None) -> None:
    document = {
        "format": "sunflowyr-loose-mod",
        "version": 3,
        "additions": additions or [],
        "name": name,
        "builtUtc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "objects": [
            {
                "file": item.payload.relative_to(final).as_posix(),
                "container": item.container,
                "pathId": item.path_id,
                "classId": item.class_id,
                "className": item.class_label,
                "name": item.name,
                "source": item.source,
                "bytes": item.size,
                "added": item.is_new,
                "resource": item.resource is not None,
            }
            for item in objects
        ],
    }
    (Path(folder) / MOD_MANIFEST).write_text(json.dumps(document, indent=2), encoding="utf-8")

def parse_descriptor(path: Path) -> dict[int, dict[str, Any]]:
    found: dict[int, dict[str, Any]] = {}

    def slot(path_id: int) -> dict[str, Any]:
        return found.setdefault(path_id, {"add": None, "fixes": [], "resource": None})

    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        words = line.split()
        try:
            if words[0] in ("append", "appendfix"):
                continue
            if words[0] == "new" and len(words) >= 5:
                slot(int(words[1]))["add"] = ("class", int(words[2]), int(words[3]), bytes.fromhex(words[4]))
            elif words[0] == "script" and len(words) >= 7:
                rest = line.split(None, 6)[6].strip()
                slot(int(words[1]))["add"] = ("script", int(words[2]), int(words[3]), bytes.fromhex(words[4]), bytes.fromhex(words[5]), rest)
            elif words[0] == "fix" and len(words) >= 4:
                slot(int(words[1]))["fixes"].append((int(words[2]), line.split(None, 3)[3].strip()))
            elif words[0] == "resource" and len(words) >= 4:
                slot(int(words[1]))["resource"] = (int(words[2]), line.split(None, 3)[3].strip())
            elif len(words) == 5:
                values = [int(word) for word in words]
                slot(values[0])["add"] = ("raw", values[1], values[2], values[3], values[4])
        except ValueError:
            continue
    return found

def read_mod_objects(folder: Path) -> list[ModObject]:
    folder = Path(folder)
    described: dict[str, dict[str, Any]] = {}
    manifest = folder / MOD_MANIFEST
    if manifest.is_file():
        try:
            for entry in json.loads(manifest.read_text(encoding="utf-8")).get("objects", []):
                if isinstance(entry, dict):
                    described[str(entry.get("file") or "").lower()] = entry
        except (OSError, ValueError, AttributeError):
            described = {}
    descriptors: dict[str, dict[int, dict[str, Any]]] = {}
    for listing in folder.rglob(DESCRIPTOR):
        descriptors[listing.parent.relative_to(folder).as_posix().lower()] = parse_descriptor(listing)
    objects: list[ModObject] = []
    for payload in sorted(folder.rglob(f"*{PAYLOAD_SUFFIX}"), key=lambda item: item.as_posix().lower()):
        path_id = payload_path_id(payload.name)
        if path_id is None or payload.parent == folder:
            continue
        container = payload.parent.relative_to(folder).as_posix()
        entry = described.get(payload.relative_to(folder).as_posix().lower(), {})
        extra = descriptors.get(container.lower(), {}).get(path_id)
        add = extra["add"] if extra else None
        class_id = int(entry.get("classId") or 0) or (add[1] if add and add[0] in ("raw", "class") else (114 if add and add[0] == "script" else 0))
        if add and add[0] == "raw":
            class_id = class_id or add[2]
        objects.append(ModObject(container, path_id, class_id, str(entry.get("name") or ""), payload, payload.stat().st_size, str(entry.get("source") or ""),
                                 add, list(extra["fixes"]) if extra else [], extra["resource"] if extra else None, extra is not None))
    return objects

def read_mod_appends(folder: Path) -> list[AppendSpec]:
    folder = Path(folder)
    found: list[AppendSpec] = []
    for listing in sorted(folder.rglob(DESCRIPTOR), key=lambda item: item.as_posix().lower()):
        container = listing.parent.relative_to(folder).as_posix()
        by_key: dict[tuple[int, int], AppendSpec] = {}
        for line in listing.read_text(encoding="utf-8", errors="replace").splitlines():
            words = line.split()
            try:
                if words and words[0] == "append" and len(words) >= 8:
                    rest = line.split(None, 7)[7].strip()
                    spec = AppendSpec(container, int(words[1]), int(words[2]), int(words[3]), int(words[4]), int(words[5]), listing.parent / rest, base_size=int(words[6]))
                    by_key[(spec.path_id, spec.number)] = spec
                    found.append(spec)
                elif words and words[0] == "appendfix" and len(words) >= 5:
                    spec = by_key.get((int(words[1]), int(words[2])))
                    if spec is not None:
                        spec.fixes.append((int(words[3]), line.split(None, 4)[4].strip()))
            except ValueError:
                continue
    return found

def read_mod_list(data_root: Path) -> list[ModEntry]:
    root = mods_root(data_root)
    folders = {path.name.lower(): path for path in sorted(root.iterdir()) if path.is_dir() and not path.name.startswith(".")} if root.is_dir() else {}
    entries: list[ModEntry] = []
    seen: set[str] = set()
    list_path = root / MOD_LIST
    listed = list_path.is_file()
    if listed:
        for line in list_path.read_text(encoding="utf-8-sig").splitlines():
            text = line.strip()
            enabled = not text.startswith("#")
            name = text.lstrip("#").strip()
            folder = folders.get(name.lower())
            if not name or name.lower() in seen or folder is None:
                continue
            seen.add(name.lower())
            entries.append(ModEntry(folder.name, enabled, folder))
    for key, folder in folders.items():
        if key not in seen:
            entries.append(ModEntry(folder.name, not listed, folder))
    for entry in entries:
        entry.objects = read_mod_objects(entry.folder)
        entry.appends = read_mod_appends(entry.folder)
    return entries

def write_mod_list(data_root: Path, entries: list[ModEntry]) -> Path:
    root = mods_root(data_root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / MOD_LIST
    target.write_text("".join((entry.name if entry.enabled else f"# {entry.name}") + "\n" for entry in entries), encoding="utf-8")
    return target

def ensure_listed(data_root: Path, name: str) -> None:
    entries = read_mod_list(data_root)
    for entry in entries:
        if entry.name.lower() == name.lower():
            entry.enabled = True
    write_mod_list(data_root, entries)

def loader_key(container: str) -> str:
    return container.replace("/", "\\").lower()

@dataclass(slots=True)
class SimSegment:
    path_id: int
    payload: Path | None = None
    size: int = 0
    found: int = 0
    add: tuple | None = None
    fixes: list[tuple[int, str]] = field(default_factory=list)
    resource: tuple[int, str] | None = None
    resolved_add: tuple | None = None
    patches: list[tuple[int, int, int]] = field(default_factory=list)
    offset: int = 0
    item: ModObject | None = None
    appends: list[AppendSpec] = field(default_factory=list)
    memory: bytes | None = None

@dataclass(slots=True)
class VirtualFile:
    container: str
    raw: bool
    real_size: int
    real_data_offset: int
    data_start: int
    virtual_size: int
    head: bytes
    segments: list[SimSegment]
    missing: list[ModObject]
    added: list[ModObject]
    replaced: list[ModObject]
    new_types: int = 0
    new_scripts: int = 0
    new_externals: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

def active_overrides(data_root: Path, entries: list[ModEntry] | None = None) -> tuple[dict[str, dict[int, SimSegment]], list[tuple[ModObject, ModObject]]]:
    tables: dict[str, dict[int, SimSegment]] = {}
    names: dict[str, str] = {}
    overridden: list[tuple[ModObject, ModObject]] = []
    for entry in entries if entries is not None else read_mod_list(data_root):
        if not entry.enabled:
            continue
        for item in entry.objects:
            key = loader_key(item.container)
            names.setdefault(key, item.container)
            table = tables.setdefault(key, {})
            segment = table.get(item.path_id)
            if segment is None:
                segment = table[item.path_id] = SimSegment(item.path_id)
            elif segment.item is not None:
                overridden.append((segment.item, item))
            segment.payload = item.payload
            segment.item = item
            if item.described:
                segment.add = item.add
                segment.fixes = list(item.fixes)
                segment.resource = item.resource
        for spec in entry.appends:
            key = loader_key(spec.container)
            names.setdefault(key, spec.container)
            table = tables.setdefault(key, {})
            segment = table.get(spec.path_id)
            if segment is None:
                segment = table[spec.path_id] = SimSegment(spec.path_id)
            segment.appends.append(spec)
    return {names[key]: table for key, table in tables.items()}, overridden

def compose_appends(real: Path, head: bytes, data_offset: int, entry: int, segment: SimSegment, resolve_external: Any, problems: list[str], container: str) -> bytes | None:
    start, size = struct.unpack_from("<II", head, entry + 8)
    with Path(real).open("rb") as handle:
        handle.seek(data_offset + start)
        original = handle.read(size)
    blobs: list[bytes | None] = []
    for spec in segment.appends:
        if spec.base_size != size:
            problems.append(f"{container} path {segment.path_id}: list addition {spec.blob.name} was built for a different version of this object, rebuild the mod")
            blobs.append(None)
            continue
        if spec.insert_offset > size or spec.count_offset + 4 > size or not spec.blob.is_file():
            problems.append(f"{container} path {segment.path_id}: list addition {spec.blob.name} doesnt fit this object")
            blobs.append(None)
            continue
        blob = bytearray(spec.blob.read_bytes())
        for offset, target in spec.fixes:
            value = resolve_external(target)
            if offset + 4 <= len(blob):
                struct.pack_into("<i", blob, offset, value)
        blobs.append(bytes(blob))
    order = sorted((index for index in range(len(segment.appends)) if blobs[index] is not None), key=lambda index: (segment.appends[index].insert_offset, index))
    out = bytearray()
    cursor = 0
    for index in order:
        insert = segment.appends[index].insert_offset
        out += original[cursor:insert]
        cursor = insert
        out += blobs[index]
    out += original[cursor:]
    for index in order:
        spec = segment.appends[index]
        shift = sum(len(blobs[other]) for other in order if segment.appends[other].insert_offset <= spec.count_offset)
        position = spec.count_offset + shift
        struct.pack_into("<i", out, position, struct.unpack_from("<i", out, position)[0] + spec.count_add)
    return bytes(out)

def simulate(data_root: Path, entries: list[ModEntry] | None = None) -> dict[str, VirtualFile]:
    data_root = Path(data_root)
    tables, overridden = active_overrides(data_root, entries)
    hosts: dict[str, list[SimSegment]] = {}
    host_names: dict[str, str] = {}
    pending: list[tuple[SimSegment, tuple[str, int]]] = []
    for container in sorted(tables, key=loader_key):
        for path_id in sorted(tables[container]):
            segment = tables[container][path_id]
            if segment.resource is None or segment.payload is None:
                continue
            host_key = loader_key(segment.resource[1])
            host_names.setdefault(host_key, segment.resource[1])
            host_list = hosts.setdefault(host_key, [])
            host_list.append(SimSegment(-(len(pending) + 1), segment.payload.with_suffix(RESOURCE_SUFFIX)))
            pending.append((segment, (host_key, len(host_list) - 1)))
    files: dict[str, VirtualFile] = {}
    host_offsets: dict[tuple[str, int], int] = {}
    for host_key, segments in hosts.items():
        path = data_root / container_folder(host_names[host_key])
        if not path.is_file():
            files[host_names[host_key]] = VirtualFile(host_names[host_key], True, 0, 0, 0, 0, b"", [], [], [], [], problems=[f"{host_names[host_key]} doesnt exist, its sounds stay silent"])
            continue
        real_size = path.stat().st_size
        append = real_size
        for index, segment in enumerate(segments):
            segment.size = segment.payload.stat().st_size
            append = (append + RESOURCE_ALIGNMENT - 1) // RESOURCE_ALIGNMENT * RESOURCE_ALIGNMENT
            segment.offset = append
            host_offsets[(host_key, index)] = append
            append += segment.size
        files[host_names[host_key]] = VirtualFile(host_names[host_key], True, real_size, 0, 0, append, b"", segments, [], [], [])
    resource_values = {id(segment): host_offsets.get(link, 0) for segment, link in pending}
    for container, table in tables.items():
        if loader_key(container) in hosts:
            continue
        files[container] = build_virtual_file(data_root, container, table, resource_values)
    return files

def parse_metadata(head: bytes) -> dict[str, Any]:
    metadata_size = struct.unpack_from(">I", head, 0)[0]
    end = 20 + metadata_size
    cursor = head.index(b"\0", 20) + 1 + 4
    type_trees = head[cursor]
    cursor += 1
    type_count_offset = cursor
    type_count = struct.unpack_from("<I", head, cursor)[0]
    cursor += 4
    types_begin = cursor
    types: list[tuple[int, int]] = []
    for unused in range(type_count):
        class_id = struct.unpack_from("<i", head, cursor)[0]
        types.append((cursor, class_id))
        cursor += 4 + (16 if class_id < 0 else 0) + 16
        if type_trees:
            node_count, string_size = struct.unpack_from("<II", head, cursor)
            cursor += 8 + node_count * 24 + string_size
    types_end = cursor
    object_count = struct.unpack_from("<i", head, cursor)[0]
    cursor += 4
    entries: list[int] = []
    for unused in range(object_count):
        cursor = (cursor + 3) & ~3
        entries.append(cursor)
        cursor += 25
    script_count = struct.unpack_from("<i", head, cursor)[0]
    cursor += 4
    scripts: list[tuple[int, int]] = []
    for unused in range(script_count):
        file_index = struct.unpack_from("<i", head, cursor)[0]
        cursor = (cursor + 4 + 3) & ~3
        scripts.append((file_index, struct.unpack_from("<q", head, cursor)[0]))
        cursor += 8
    external_count = struct.unpack_from("<i", head, cursor)[0]
    cursor += 4
    externals_begin = cursor
    externals: list[str] = []
    for unused in range(external_count):
        cursor = head.index(b"\0", cursor) + 1 + 16 + 4
        stop = head.index(b"\0", cursor)
        externals.append(head[cursor:stop].decode("utf-8", "replace"))
        cursor = stop + 1
    return {
        "metadata_size": metadata_size, "end": end, "type_trees": type_trees, "type_count_offset": type_count_offset,
        "types_begin": types_begin, "types_end": types_end, "types": types, "entries": entries, "scripts": scripts,
        "externals_begin": externals_begin, "externals_end": cursor, "externals": externals,
    }

def build_virtual_file(data_root: Path, container: str, table: dict[int, SimSegment], resource_values: dict[int, int | None]) -> VirtualFile:
    path = data_root / container_folder(container)
    if not path.is_file():
        raise LooseFormatError(f"{container} isnt in {data_root}")
    with path.open("rb") as handle:
        header = handle.read(20)
        metadata_size, stored_size, version, data_offset = struct.unpack(">IIII", header[:16])
        if version != 15 or header[16] != 0:
            raise LooseFormatError(f"{container} uses serialized format {version}, the loader reads only format 15")
        handle.seek(0)
        head = handle.read(data_offset)
    real_size = path.stat().st_size
    meta = parse_metadata(head)
    present = {struct.unpack_from("<q", head, offset)[0]: index + 1 for index, offset in enumerate(meta["entries"])}
    segments = [table[path_id] for path_id in sorted(table)]
    externals = list(meta["externals"])
    new_externals: list[str] = []
    scripts = list(meta["scripts"])
    script_base = len(scripts)
    new_types: list[tuple[int, bytes | None, bytes]] = []
    missing: list[ModObject] = []
    problems: list[str] = []

    def resolve_external(name: str) -> int:
        if not name or name == SAME_FILE:
            return 0
        for index, existing in enumerate(externals):
            if existing.lower() == name.lower():
                return index + 1
        for index, existing in enumerate(new_externals):
            if existing.lower() == name.lower():
                return len(externals) + index + 1
        new_externals.append(name)
        return len(externals) + len(new_externals)

    def resolve_script(file_index: int, path_id: int) -> int:
        for index, value in enumerate(scripts):
            if value == (file_index, path_id):
                return index
        scripts.append((file_index, path_id))
        return len(scripts) - 1

    def resolve_class_type(class_id: int, type_hash: bytes) -> int:
        if any(existing == class_id for offset, existing in meta["types"]) or any(value[0] == class_id for value in new_types):
            return class_id
        new_types.append((class_id, None, type_hash))
        return class_id

    def resolve_script_type(script_id: bytes, type_hash: bytes) -> int:
        lowest = 0
        for offset, existing in meta["types"]:
            if existing >= 0:
                continue
            lowest = min(lowest, existing)
            if head[offset + 4: offset + 20] == script_id:
                return existing
        for value in new_types:
            lowest = min(lowest, value[0])
            if value[1] == script_id:
                return value[0]
        new_types.append((lowest - 1, script_id, type_hash))
        return lowest - 1

    for segment in segments:
        segment.found = present.get(segment.path_id, 0)
        segment.size = 0
        segment.resolved_add = None
        segment.patches = []
        segment.memory = None
        if segment.appends and segment.found:
            if segment.payload is not None:
                problems.append(f"{container} path {segment.path_id}: a mod replaces this object whole, so list additions to it are skipped")
            else:
                segment.memory = compose_appends(path, head, data_offset, meta["entries"][segment.found - 1], segment, resolve_external, problems, container)
                segment.size = len(segment.memory)
        if segment.memory is None and segment.payload is None and not segment.found:
            continue
        if not segment.found and segment.add is None:
            missing.append(segment.item)
            continue
        if segment.memory is None and (segment.payload is None or not segment.payload.is_file()):
            problems.append(f"{container} path {segment.path_id}: payload file missing")
            continue
        if segment.memory is None:
            segment.size = segment.payload.stat().st_size
        if not segment.found:
            add = segment.add
            if add[0] == "raw":
                segment.resolved_add = (add[1], add[2], add[3], add[4])
            elif add[0] == "class":
                segment.resolved_add = (resolve_class_type(add[1], add[3]), add[1], -1, add[2])
            else:
                file_index = resolve_external(add[5])
                script_index = resolve_script(file_index, add[2])
                segment.resolved_add = (resolve_script_type(add[3], add[4]), 114, script_index, add[1])
        for offset, target in segment.fixes:
            value = resolve_external(target)
            if offset + 4 <= segment.size:
                segment.patches.append((offset, 4, value))
        if segment.resource is not None:
            value = resource_values.get(id(segment))
            if value is not None and segment.resource[0] + 8 <= segment.size:
                segment.patches.append((segment.resource[0], 8, value))

    out = bytearray(head[20: meta["type_count_offset"]])
    out += struct.pack("<I", len(meta["types"]) + len(new_types))
    out += head[meta["types_begin"]: meta["types_end"]]
    for class_id, script_id, type_hash in new_types:
        out += struct.pack("<i", class_id) + (script_id or b"") + type_hash
        if meta["type_trees"]:
            out += b"\0" * 8

    def align() -> None:
        out.extend(b"\0" * ((-(20 + len(out))) % 4))

    new_segments = [segment for segment in segments if not segment.found and segment.resolved_add is not None and segment.size]
    out += struct.pack("<i", len(meta["entries"]) + len(new_segments))
    slots: dict[int, int] = {}
    for index, offset in enumerate(meta["entries"]):
        align()
        slots[index + 1] = len(out)
        out += head[offset: offset + 25]
    new_slots: dict[int, int] = {}
    for segment in new_segments:
        align()
        new_slots[segment.path_id] = len(out)
        type_id, class_id, script_index, stripped = segment.resolved_add
        out += struct.pack("<qIIiHhB", segment.path_id, 0, 0, type_id, class_id, script_index, stripped)
    out += struct.pack("<i", len(scripts))
    for file_index, path_id in scripts:
        out += struct.pack("<i", file_index)
        align()
        out += struct.pack("<q", path_id)
    out += struct.pack("<i", len(externals) + len(new_externals))
    out += head[meta["externals_begin"]: meta["externals_end"]]
    for name in new_externals:
        out += b"\0" * 21 + name.encode("utf-8") + b"\0"
    out += head[meta["externals_end"]: meta["end"]]
    data_start = data_offset
    if 20 + len(out) > data_start:
        data_start = (20 + len(out) + 15) // 16 * 16
    append = data_start + real_size - data_offset
    placed: list[SimSegment] = []
    replaced: list[ModObject] = []
    added: list[ModObject] = []
    for want_new in (False, True):
        for segment in segments:
            if not segment.size:
                continue
            is_new = not segment.found and segment.resolved_add is not None
            if is_new != want_new or (not segment.found and not is_new):
                continue
            relative = (append - data_start + OBJECT_ALIGNMENT - 1) // OBJECT_ALIGNMENT * OBJECT_ALIGNMENT
            if relative + segment.size > SIZE_LIMIT:
                raise LooseFormatError(f"{container} would pass 4 GB")
            segment.offset = data_start + relative
            append = segment.offset + segment.size
            slot = new_slots[segment.path_id] if is_new else slots[segment.found]
            struct.pack_into("<II", out, slot + 8, relative, segment.size)
            placed.append(segment)
            (added if is_new else replaced).append(segment.item)
    if append > SIZE_LIMIT:
        raise LooseFormatError(f"{container} would pass 4 GB")
    built = bytearray(header) + out
    built.extend(b"\0" * (data_start - len(built)))
    struct.pack_into(">II", built, 0, len(out), append)
    struct.pack_into(">I", built, 12, data_start)
    return VirtualFile(container, False, real_size, data_offset, data_start, append, bytes(built), placed, missing, added, replaced,
                       len(new_types), len(scripts) - script_base, new_externals, problems)

def virtual_bytes(data_root: Path, layout: VirtualFile) -> bytes:
    real = (Path(data_root) / container_folder(layout.container)).read_bytes()
    output = bytearray(layout.head)
    output.extend(real[layout.real_data_offset:])
    output.extend(b"\0" * (layout.virtual_size - len(output)))
    for segment in layout.segments:
        data = bytearray(segment.memory if segment.memory is not None else segment.payload.read_bytes())
        for offset, width, value in segment.patches:
            data[offset: offset + width] = value.to_bytes(width, "little")
        output[segment.offset: segment.offset + segment.size] = data
    return bytes(output)
