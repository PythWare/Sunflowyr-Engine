from __future__ import annotations
import hashlib, struct
from dataclasses import dataclass
from typing import Iterable
from .game_script_hashes import lookup as lookup_script_hashes
from .game_script_hashes import lookup_properties as lookup_script_properties
from .game_type_hashes import GAME_TYPE_HASHES

UNITY_VERSION = "5.4.1p4"
UNITY_GENERATION = "5.x.x"
SERIALIZED_FORMAT_VERSION = 15
WINDOWS_PLATFORM = 5
ASSET_BUNDLE_CLASS_ID = 142
ASSET_BUNDLE_TYPE_HASH = bytes.fromhex("6974f6c74321933ec4ba7437a55be2c3")
AUDIO_CLIP_CLASS_ID = 83
AUDIO_CLIP_TYPE_HASH = bytes.fromhex("e5f31cd4b52fedfd9e332593b92efbf2")
TEXTURE2D_CLASS_ID = 28
TEXTURE2D_TYPE_HASH = bytes.fromhex("2f903e13950e7204e5adb9d0359bd5b2")
MESH_CLASS_ID = 43
MESH_TYPE_HASH = bytes.fromhex("7487684e77cbd197aaf389864da1d8c7")
TEXT_ASSET_CLASS_ID = 49
TEXT_ASSET_TYPE_HASH = bytes.fromhex("936739f45f26fd8b81a4ea70f2c8cad4")
ANIMATION_CLIP_CLASS_ID = 74
ANIMATION_CLIP_TYPE_HASH = bytes.fromhex("d8e8eeb43589ccd7019398e56f6c16b0")
FONT_CLASS_ID = 128
FONT_TYPE_HASH = bytes.fromhex("caad7e89f39acbffc73bf3cae4b1433f")
PHYSIC_MATERIAL_CLASS_ID = 134
PHYSIC_MATERIAL_TYPE_HASH = bytes.fromhex("3fffe3ab5176cce0865f5ff4f2fa427d")
MATERIAL_CLASS_ID = 21
MATERIAL_TYPE_HASH = bytes.fromhex("1209f97dc24e29870a0b4d4c10691c51")
SPRITE_CLASS_ID = 213
SPRITE_TYPE_HASH = bytes.fromhex("275179f2d868a3705813013ae3b8fe22")
GAME_OBJECT_CLASS_ID = 1
GAME_OBJECT_TYPE_HASH = bytes.fromhex("40da10691c0dcc34b5660762da133b32")
TRANSFORM_CLASS_ID = 4
TRANSFORM_TYPE_HASH = bytes.fromhex("937a6b5c5f5b313185d252e38d145b17")
MESH_RENDERER_CLASS_ID = 23
MESH_RENDERER_TYPE_HASH = bytes.fromhex("bea915a8ab3d9b55e80c969a22e692d3")
MESH_FILTER_CLASS_ID = 33
MESH_FILTER_TYPE_HASH = bytes.fromhex("9d1630b1d7e3bc24abe46e5860a2a179")
SKINNED_MESH_RENDERER_CLASS_ID = 137
SKINNED_MESH_RENDERER_TYPE_HASH = bytes.fromhex("6932d6d1d46264c8680a181056f98be2")
RECT_TRANSFORM_CLASS_ID = 224
MONO_BEHAVIOUR_CLASS_ID = 114
MONO_SCRIPT_CLASS_ID = 115
MONO_SCRIPT_TYPE_HASH = bytes.fromhex("2da9c96f9efc8b31a480d5b8858755f3")
TYPE_HASHES = {
    ASSET_BUNDLE_CLASS_ID: ASSET_BUNDLE_TYPE_HASH,
    AUDIO_CLIP_CLASS_ID: AUDIO_CLIP_TYPE_HASH,
    TEXTURE2D_CLASS_ID: TEXTURE2D_TYPE_HASH,
    MESH_CLASS_ID: MESH_TYPE_HASH,
    TEXT_ASSET_CLASS_ID: TEXT_ASSET_TYPE_HASH,
    ANIMATION_CLIP_CLASS_ID: ANIMATION_CLIP_TYPE_HASH,
    FONT_CLASS_ID: FONT_TYPE_HASH,
    PHYSIC_MATERIAL_CLASS_ID: PHYSIC_MATERIAL_TYPE_HASH,
    MATERIAL_CLASS_ID: MATERIAL_TYPE_HASH,
    SPRITE_CLASS_ID: SPRITE_TYPE_HASH,
    GAME_OBJECT_CLASS_ID: GAME_OBJECT_TYPE_HASH,
    TRANSFORM_CLASS_ID: TRANSFORM_TYPE_HASH,
    MESH_RENDERER_CLASS_ID: MESH_RENDERER_TYPE_HASH,
    MESH_FILTER_CLASS_ID: MESH_FILTER_TYPE_HASH,
    SKINNED_MESH_RENDERER_CLASS_ID: SKINNED_MESH_RENDERER_TYPE_HASH,
    MONO_SCRIPT_CLASS_ID: MONO_SCRIPT_TYPE_HASH,
}

TYPE_HASHES = {**GAME_TYPE_HASHES, **TYPE_HASHES}

@dataclass(frozen=True, slots=True)
class ScriptTypeRef:

    assembly: str
    namespace: str
    class_name: str
    path_id: int

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.assembly, self.namespace, self.class_name)

    def game_hashes(self) -> tuple[bytes, bytes] | None:
        return lookup_script_hashes(self.assembly, self.namespace, self.class_name)

    def type_hash(self) -> bytes:
        found = self.game_hashes()
        if found is not None:
            return found[1]
        digest = hashlib.md5()
        digest.update(b"sunflowyr-monobehaviour\0")
        digest.update("/".join(self.key).encode("utf-8"))
        return digest.digest()

    def properties_hash(self) -> bytes:
        found = self.game_hashes()
        if found is not None:
            return found[0]
        digest = hashlib.md5()
        digest.update(self.assembly.encode("utf-8"))
        digest.update(b"\0")
        digest.update(self.namespace.encode("utf-8"))
        digest.update(b"\0")
        digest.update(self.class_name.encode("utf-8"))
        return digest.digest()

    def monoscript_properties_hash(self) -> bytes:
        found = lookup_script_properties(self.assembly, self.namespace, self.class_name)
        if found is not None:
            return found
        return self.type_hash()

class AssetBundleWriteError(ValueError):
    pass

@dataclass(frozen=True, slots=True)
class BundleObject:
    path_id: int
    class_id: int
    asset_path: str
    name: str
    payload: bytes
    script: ScriptTypeRef | None = None
    container_listed: bool = True
    preload_span: int = 1

@dataclass(frozen=True, slots=True)
class BundleNodeData:
    path: str
    data: bytes
    flags: int

def build_asset_bundle_bytes(
    bundle_name: str,
    cab_name: str,
    objects: Iterable[BundleObject],
    resource_name: str,
    resource_data: bytes,
) -> tuple[bytes, bytes]:
    object_list = list(objects)
    if not object_list:
        raise AssetBundleWriteError("an AssetBundle needs at least one asset")
    validate_bundle_objects(object_list)
    serialized = build_serialized_file(bundle_name, object_list)
    nodes = [BundleNodeData(cab_name, serialized, 4)]
    if resource_data:
        nodes.append(BundleNodeData(resource_name, bytes(resource_data), 0))
    return build_unity_fs(nodes), serialized

def validate_bundle_objects(objects: list[BundleObject]) -> None:
    path_ids: set[int] = set()
    asset_paths: set[str] = set()
    for item in objects:
        if item.path_id <= 1:
            raise AssetBundleWriteError("generated asset path IDs must be greater than 1")
        if item.path_id in path_ids:
            raise AssetBundleWriteError(f"duplicate generated path ID {item.path_id}")
        path_ids.add(item.path_id)
        normalized_path = item.asset_path.replace("\\", "/").strip("/")
        if item.container_listed:
            if not normalized_path:
                raise AssetBundleWriteError("generated asset path cant be empty")
            path_key = normalized_path.casefold()
            if path_key in asset_paths:
                raise AssetBundleWriteError(f"duplicate AssetBundle path {normalized_path}")
            asset_paths.add(path_key)
        if item.script is None and item.class_id not in TYPE_HASHES:
            raise AssetBundleWriteError(
                f"class {item.class_id} has no RWBY GE AssetBundle type definition"
            )
        if item.script is not None and item.class_id != MONO_BEHAVIOUR_CLASS_ID:
            raise AssetBundleWriteError(
                f"class {item.class_id} cant carry a MonoBehaviour script reference"
            )

def build_serialized_file(bundle_name: str, objects: list[BundleObject]) -> bytes:
    bundle_payload = build_asset_bundle_object(bundle_name, objects)
    serialized_objects = [
        BundleObject(
            path_id=1,
            class_id=ASSET_BUNDLE_CLASS_ID,
            asset_path="",
            name=bundle_name,
            payload=bundle_payload,
        ),
        *objects,
    ]
    data = bytearray()
    object_records: list[tuple[BundleObject, int, int]] = []
    for item in serialized_objects:
        align_buffer(data, 8)
        relative_start = len(data)
        data.extend(item.payload)
        object_records.append((item, relative_start, len(item.payload)))

    script_refs: list[ScriptTypeRef] = []
    script_type_ids: dict[tuple[str, str, str], int] = {}
    for item in serialized_objects:
        if item.script is None:
            continue
        if item.script.key in script_type_ids:
            continue
        script_type_ids[item.script.key] = -(len(script_refs) + 1)
        script_refs.append(item.script)

    type_ids = sorted({item.class_id for item in serialized_objects if item.script is None})
    metadata = bytearray()
    metadata.extend(UNITY_VERSION.encode("ascii") + b"\0")
    metadata.extend(struct.pack("<i", WINDOWS_PLATFORM))
    metadata.extend(b"\0")
    metadata.extend(struct.pack("<i", len(type_ids) + len(script_refs)))
    for class_id in type_ids:
        metadata.extend(struct.pack("<i", class_id))
        metadata.extend(TYPE_HASHES[class_id])
    for reference in script_refs:
        metadata.extend(struct.pack("<i", script_type_ids[reference.key]))
        metadata.extend(reference.properties_hash())
        metadata.extend(reference.type_hash())

    script_table = [reference.path_id for reference in script_refs]
    metadata.extend(struct.pack("<i", len(object_records)))
    for item, relative_start, byte_size in object_records:
        align_buffer_with_base(metadata, 4, 20)
        if item.script is not None:
            type_id = script_type_ids[item.script.key]
            script_index = script_table.index(item.script.path_id)
        else:
            type_id = item.class_id
            script_index = -1
        metadata.extend(struct.pack("<q", item.path_id))
        metadata.extend(struct.pack("<I", relative_start))
        metadata.extend(struct.pack("<I", byte_size))
        metadata.extend(struct.pack("<i", type_id))
        metadata.extend(struct.pack("<H", item.class_id))
        metadata.extend(struct.pack("<h", script_index))
        metadata.extend(b"\0")

    metadata.extend(struct.pack("<i", len(script_table)))
    for script_path_id in script_table:
        metadata.extend(struct.pack("<i", 0))
        align_buffer_with_base(metadata, 4, 20)
        metadata.extend(struct.pack("<q", script_path_id))
    metadata.extend(struct.pack("<i", 0))
    metadata.extend(b"\0")

    metadata_size = 20 + len(metadata)
    data_offset = align_value(metadata_size, 16)
    file_size = data_offset + len(data)
    if file_size > 0xFFFFFFFF:
        raise AssetBundleWriteError("Unity format 15 files cant exceed 4 GB")
    header = struct.pack(
        ">IIII",
        metadata_size,
        file_size,
        SERIALIZED_FORMAT_VERSION,
        data_offset,
    ) + b"\0\0\0\0"
    padding = b"\0" * (data_offset - metadata_size)
    return header + bytes(metadata) + padding + bytes(data)

def build_asset_bundle_object(bundle_name: str, objects: list[BundleObject]) -> bytes:
    output = bytearray()
    output.extend(pack_aligned_string(bundle_name))
    preload_order = sorted(
        range(len(objects)),
        key=lambda index: 0 if objects[index].class_id == MONO_SCRIPT_CLASS_ID else 1,
    )
    script_span = sum(1 for item in objects if item.class_id == MONO_SCRIPT_CLASS_ID)
    output.extend(struct.pack("<i", len(objects)))
    for index in preload_order:
        output.extend(pack_pointer(objects[index].path_id))
    listed = [item for item in objects if item.container_listed]
    output.extend(struct.pack("<i", len(listed)))
    for item in listed:
        output.extend(pack_aligned_string(item.asset_path.replace("\\", "/")))
        output.extend(struct.pack("<ii", 0, script_span))
        output.extend(pack_pointer(item.path_id))

    output.extend(struct.pack("<ii", 0, 0))
    output.extend(pack_pointer(0))
    output.extend(struct.pack("<i", 0))
    output.extend(struct.pack("<I", 1))
    output.extend(pack_aligned_string(bundle_name))
    output.extend(struct.pack("<i", 0))
    output.extend(b"\0")
    align_buffer(output, 4)
    return bytes(output)

def build_unity_fs(nodes: list[BundleNodeData]) -> bytes:
    if not nodes:
        raise AssetBundleWriteError("UnityFS archive needs at least one node")
    block_data = bytearray()
    node_records: list[tuple[int, int, BundleNodeData]] = []
    for node in nodes:
        offset = len(block_data)
        block_data.extend(node.data)
        node_records.append((offset, len(node.data), node))

    if len(block_data) > 0xFFFFFFFF:
        raise AssetBundleWriteError("UnityFS block exceeds the 32 bit block limit")
    info = bytearray(16)
    info.extend(struct.pack(">i", 1))
    info.extend(struct.pack(">IIH", len(block_data), len(block_data), 0x40))
    info.extend(struct.pack(">i", len(node_records)))
    for offset, size, node in node_records:
        info.extend(struct.pack(">qqI", offset, size, node.flags))
        info.extend(node.path.encode("utf-8") + b"\0")

    header_prefix = bytearray()
    header_prefix.extend(b"UnityFS\0")
    header_prefix.extend(struct.pack(">I", 6))
    header_prefix.extend(UNITY_GENERATION.encode("ascii") + b"\0")
    header_prefix.extend(UNITY_VERSION.encode("ascii") + b"\0")
    header_size = len(header_prefix) + 20
    bundle_size = header_size + len(info) + len(block_data)
    header_prefix.extend(struct.pack(">qIII", bundle_size, len(info), len(info), 0x40))
    return bytes(header_prefix) + bytes(info) + bytes(block_data)

def pack_pointer(path_id: int) -> bytes:
    return struct.pack("<iq", 0, path_id)

def pack_aligned_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    output = bytearray(struct.pack("<i", len(encoded)))
    output.extend(encoded)
    align_buffer(output, 4)
    return bytes(output)

def align_buffer(buffer: bytearray, alignment: int) -> None:
    padding = (-len(buffer)) % alignment
    if padding:
        buffer.extend(b"\0" * padding)

def align_buffer_with_base(buffer: bytearray, alignment: int, base: int) -> None:
    padding = (-(base + len(buffer))) % alignment
    if padding:
        buffer.extend(b"\0" * padding)

def align_value(value: int, alignment: int) -> int:
    return (int(value) + alignment - 1) // alignment * alignment
