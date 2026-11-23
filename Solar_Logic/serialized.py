from __future__ import annotations
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from .binary import EndianReader
from .class_ids import class_name

@dataclass(slots=True)
class SerializedType:
    class_id: int

@dataclass(slots=True)
class SerializedObject:
    path_id: int
    byte_start: int
    byte_size: int
    type_id: int
    class_id: int
    entry_offset: int
    path_id_offset: int
    byte_start_field_offset: int
    byte_size_field_offset: int
    script_type_index: int | None = None
    stripped: int | None = None

    @property
    def type_name(self) -> str:
        return class_name(self.class_id)

@dataclass(slots=True)
class SerializedAsset:
    path: Path
    data_root: Path
    metadata_size: int
    file_size: int
    format_version: int
    data_offset: int
    endian: str
    unity_version: str
    platform: int
    enable_type_tree: bool
    types: list[SerializedType]
    objects: list[SerializedObject]
    externals: list[str]
    user_information: str

    @property
    def relative_path(self) -> str:
        try:
            return self.path.relative_to(self.data_root).as_posix()
        except ValueError:
            return self.path.name

    def class_counts(self) -> Counter:
        return Counter(obj.class_id for obj in self.objects)

def is_serialized_file(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 20:
        return False
    with path.open("rb") as handle:
        reader = EndianReader(handle, ">")
        try:
            metadata_size = reader.u32()
            file_size = reader.u32()
            version = reader.u32()
            data_offset = reader.u32()
            reader.u8()
            reader.read(3)
            if version >= 22:
                if path.stat().st_size < 48:
                    return False
                metadata_size = reader.u32()
                file_size = reader.i64()
                data_offset = reader.i64()
            actual_size = path.stat().st_size
            return file_size == actual_size and 0 < data_offset <= actual_size and metadata_size > 0
        except Exception:
            return False

def discover_serialized_files(data_root: Path) -> list[Path]:
    return sorted(
        (path for path in data_root.rglob("*") if is_serialized_file(path)),
        key=lambda path: path.relative_to(data_root).as_posix().lower(),
    )

ASSET_CACHE_SIZE = 16
asset_cache: dict[tuple[str, str, int, int], SerializedAsset] = {}

def load_serialized_asset(path: Path, data_root: Path | None = None) -> SerializedAsset:
    path = Path(path)
    root = Path(data_root) if data_root is not None else path.parent
    stat = path.stat()
    key = (str(path), str(root), stat.st_size, stat.st_mtime_ns)
    cached = asset_cache.pop(key, None)
    if cached is None:
        with path.open("rb") as handle:
            cached = read_serialized_asset(handle, path, root)
    asset_cache[key] = cached
    while len(asset_cache) > ASSET_CACHE_SIZE:
        del asset_cache[next(iter(asset_cache))]
    return cached

def read_serialized_asset(handle: BinaryIO, path: Path, data_root: Path) -> SerializedAsset:
    reader = EndianReader(handle, ">")
    metadata_size = reader.u32()
    file_size = reader.u32()
    format_version = reader.u32()
    data_offset = reader.u32()

    if format_version >= 9:
        endian_byte = reader.u8()
        reader.read(3)
        file_endian = "<" if endian_byte == 0 else ">"
    else:
        reader.seek(file_size - metadata_size)
        endian_byte = reader.u8()
        file_endian = "<" if endian_byte == 0 else ">"

    reader.endian = file_endian

    if format_version >= 22:
        metadata_size = reader.u32()
        file_size = reader.i64()
        data_offset = reader.i64()
        reader.i64()

    unity_version = "2.5.0f5"
    if format_version >= 7:
        unity_version = reader.cstring()

    platform = -2
    if format_version >= 8:
        platform = reader.i32()

    enable_type_tree = True
    if format_version >= 13:
        enable_type_tree = reader.bool()

    type_count = reader.i32()
    types = [read_serialized_type(reader, format_version, enable_type_tree, False) for _ in range(type_count)]

    big_id_enabled = 0
    if 7 <= format_version < 14:
        big_id_enabled = reader.i32()

    object_count = reader.i32()
    objects: list[SerializedObject] = []
    for _ in range(object_count):
        entry_offset = reader.tell()
        if big_id_enabled:
            path_id_offset = reader.tell()
            path_id = reader.i64()
        elif format_version < 14:
            path_id_offset = reader.tell()
            path_id = reader.i32()
        else:
            reader.align()
            path_id_offset = reader.tell()
            path_id = reader.i64()

        byte_start_field_offset = reader.tell()
        if format_version >= 22:
            byte_start = reader.i64()
        else:
            byte_start = reader.u32()
        byte_start += data_offset

        byte_size_field_offset = reader.tell()
        byte_size = reader.u32()
        type_id = reader.i32()
        if format_version < 16:
            class_id = reader.u16()
        else:
            class_id = types[type_id].class_id

        if format_version < 11:
            reader.u16()

        script_type_index = None
        if 11 <= format_version < 17:
            script_type_index = reader.i16()

        stripped = None
        if format_version in (15, 16):
            stripped = reader.u8()

        objects.append(
            SerializedObject(
                path_id=path_id,
                byte_start=byte_start,
                byte_size=byte_size,
                type_id=type_id,
                class_id=class_id,
                entry_offset=entry_offset,
                path_id_offset=path_id_offset,
                byte_start_field_offset=byte_start_field_offset,
                byte_size_field_offset=byte_size_field_offset,
                script_type_index=script_type_index,
                stripped=stripped,
            )
        )

    if format_version >= 11:
        script_count = reader.i32()
        for _ in range(script_count):
            reader.i32()
            if format_version < 14:
                reader.i32()
            else:
                reader.align()
                reader.i64()

    externals: list[str] = []
    external_count = reader.i32()
    for _ in range(external_count):
        if format_version >= 6:
            reader.cstring()
        if format_version >= 5:
            reader.read(16)
            reader.i32()
        externals.append(reader.cstring())

    ref_type_min_version = 20
    if format_version >= ref_type_min_version:
        ref_count = reader.i32()
        for _ in range(ref_count):
            read_serialized_type(reader, format_version, enable_type_tree, True)

    user_information = ""
    if format_version >= 5:
        user_information = reader.cstring()

    return SerializedAsset(
        path=path,
        data_root=data_root,
        metadata_size=metadata_size,
        file_size=file_size,
        format_version=format_version,
        data_offset=data_offset,
        endian=file_endian,
        unity_version=unity_version,
        platform=platform,
        enable_type_tree=enable_type_tree,
        types=types,
        objects=objects,
        externals=externals,
        user_information=user_information,
    )

def read_serialized_type(
    reader: EndianReader,
    format_version: int,
    enable_type_tree: bool,
    is_ref_type: bool,
) -> SerializedType:
    class_id = reader.i32()

    if format_version >= 16:
        reader.bool()

    script_type_index = -1
    if format_version >= 17:
        script_type_index = reader.i16()

    if format_version >= 13:
        if is_ref_type and script_type_index >= 0:
            reader.read(16)
        elif (format_version < 16 and class_id < 0) or (format_version >= 16 and class_id == 114):
            reader.read(16)
        reader.read(16)

    if enable_type_tree:
        if format_version >= 12 or format_version == 10:
            node_count = reader.i32()
            string_buffer_size = reader.i32()
            node_size = 32 if format_version >= 19 else 24
            reader.read(node_count * node_size)
            reader.read(string_buffer_size)
        else:
            skip_old_type_tree(reader, format_version)

        stores_type_dependencies_version = 17
        if format_version >= stores_type_dependencies_version:
            if is_ref_type:
                reader.cstring()
                reader.cstring()
                reader.cstring()
            else:
                dependency_count = reader.i32()
                reader.read(dependency_count * 4)

    return SerializedType(class_id=class_id)

def skip_old_type_tree(reader: EndianReader, format_version: int) -> None:
    reader.cstring()
    reader.cstring()
    reader.i32()
    if format_version == 2:
        reader.i32()
    if format_version != 3:
        reader.i32()
    reader.i32()
    reader.i32()
    if format_version != 3:
        reader.i32()
    children = reader.i32()
    for _ in range(children):
        skip_old_type_tree(reader, format_version)
