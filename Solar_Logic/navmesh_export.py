from __future__ import annotations
import json, struct
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from .binary import EndianReader
from .class_ids import class_name
from .model_context import ModelContextIndex
from .serialized import SerializedAsset, SerializedObject
from .unity_typetree_codec import TypeTreeCodecError, TypeTreeNode, load_schema, read_value

ReservePath = Callable[[Path, str], Path]
NAVMESH_DATA_SCHEMA = "navmesh_data_schema.json"
NAVMESH_TRAILING_FIELDS = ("m_Heightmaps", "m_HeightMeshes", "m_OffMeshLinks")
NAVMESH_MAGIC = 0x444E4156
NAVMESH_VERSION = 16
NAVMESH_TILE_HEADER_SIZE = 72
NAVMESH_VERTEX_SIZE = 12
NAVMESH_POLYGON_SIZE = 32
NAVMESH_DETAIL_MESH_SIZE = 12
NAVMESH_DETAIL_VERTEX_SIZE = 12
NAVMESH_DETAIL_TRIANGLE_SIZE = 8
NAVMESH_BV_NODE_SIZE = 16
NAVMESH_VERTS_PER_POLYGON = 6

class NavMeshExportError(Exception):
    pass

@dataclass(slots=True)
class NavMeshDataRecord:
    name: str
    positions: array
    indices: array
    document: dict
    tile_blob_data: bytes

@dataclass(slots=True)
class NavMeshSidecarResult:
    json_path: Path
    tile_blob_path: Path
    document: dict
    metrics: dict

def read_navmesh_data(
    context: ModelContextIndex,
    asset: SerializedAsset,
    obj: SerializedObject,
) -> NavMeshDataRecord:
    positions = array("f")
    indices = array("I")
    tile_blob_data = bytearray()
    tile_documents: list[dict] = []
    polygon_vertex_counts = [0] * 7
    area_counts: dict[str, int] = {}
    flag_counts: dict[str, int] = {}
    min_bounds = [float("inf"), float("inf"), float("inf")]
    max_bounds = [float("-inf"), float("-inf"), float("-inf")]

    with asset.path.open("rb") as handle:
        reader = EndianReader(handle, asset.endian)
        reader.seek(obj.byte_start)
        object_end = obj.byte_start + obj.byte_size
        name = reader.aligned_string(max_length=4096)
        tile_count = read_count(reader, 1_000_000, "NavMesh tile")
        for tile_index in range(tile_count):
            tile_size = read_count(reader, 512 * 1024 * 1024, "NavMesh tile byte")
            tile_data = reader.read(tile_size)
            reader.align()
            tile_offset = len(tile_blob_data)
            tile_blob_data.extend(tile_data)
            tile = parse_navmesh_tile(tile_data, tile_index)
            vertex_base = len(positions) // 3
            for x, y, z in tile["vertices"]:
                positions.extend((-x, y, z))
                min_bounds[0] = min(min_bounds[0], x)
                min_bounds[1] = min(min_bounds[1], y)
                min_bounds[2] = min(min_bounds[2], z)
                max_bounds[0] = max(max_bounds[0], x)
                max_bounds[1] = max(max_bounds[1], y)
                max_bounds[2] = max(max_bounds[2], z)
            for polygon in tile["polygons"]:
                vertex_count = polygon["vertexCount"]
                polygon_vertex_counts[vertex_count] += 1
                area_key = str(polygon["area"])
                flag_key = str(polygon["flags"])
                area_counts[area_key] = area_counts.get(area_key, 0) + 1
                flag_counts[flag_key] = flag_counts.get(flag_key, 0) + 1
                polygon_indices = polygon["vertices"]
                for triangle_index in range(1, vertex_count - 1):
                    indices.extend(
                        (
                            vertex_base + polygon_indices[0],
                            vertex_base + polygon_indices[triangle_index + 1],
                            vertex_base + polygon_indices[triangle_index],
                        )
                    )
            tile_documents.append(
                {
                    "tileIndex": tile_index,
                    "x": tile["x"],
                    "y": tile["y"],
                    "layer": tile["layer"],
                    "vertexCount": tile["vertexCount"],
                    "polygonCount": tile["polygonCount"],
                    "triangleCount": tile["triangleCount"],
                    "detailMeshCount": tile["detailMeshCount"],
                    "detailVertexCount": tile["detailVertexCount"],
                    "detailTriangleCount": tile["detailTriangleCount"],
                    "bvNodeCount": tile["bvNodeCount"],
                    "boundsMin": vector_document(tile["boundsMin"]),
                    "boundsMax": vector_document(tile["boundsMax"]),
                    "bvQuantFactor": tile["bvQuantFactor"],
                    "blob": {
                        "binaryRole": "navmeshTiles",
                        "byteOffset": tile_offset,
                        "byteLength": tile_size,
                    },
                }
            )

        navmesh_params = {
            "tileSize": reader.f32(),
            "walkableHeight": reader.f32(),
            "walkableRadius": reader.f32(),
            "walkableClimb": reader.f32(),
            "cellSize": reader.f32(),
        }
        trailing: dict[str, list] = {}
        for field_name in NAVMESH_TRAILING_FIELDS:
            try:
                trailing[field_name] = read_value(navmesh_schema_field(field_name), reader)
            except (TypeTreeCodecError, ValueError, EOFError) as exc:
                raise NavMeshExportError(f"NavMeshData {field_name} couldnt be read: {exc}") from exc
        if reader.tell() != object_end:
            raise NavMeshExportError(
                f"NavMeshData parser ended at 0x{reader.tell():x}, expected 0x{object_end:x}"
            )

    navmesh_key = context.key_for(asset, obj.path_id)
    settings = [
        {
            "container": item.asset_key,
            "pathId": item.key[1],
            "navMeshData": {
                "fileId": item.navmesh_data.file_id,
                "pathId": item.navmesh_data.path_id,
            },
        }
        for item in context.navmesh_settings_by_data.get(navmesh_key, [])
    ]
    total_polygons = sum(tile["polygonCount"] for tile in tile_documents)
    total_triangles = len(indices) // 3
    bounds_min = min_bounds if positions else [0.0, 0.0, 0.0]
    bounds_max = max_bounds if positions else [0.0, 0.0, 0.0]
    document = {
        "format": "sunflowyr-navmesh-data",
        "version": 1,
        "game": "rwby-ge",
        "unityVersion": asset.unity_version,
        "source": {
            "container": asset.relative_path,
            "pathId": obj.path_id,
            "classId": obj.class_id,
            "className": class_name(obj.class_id),
            "byteOffset": obj.byte_start,
            "byteSize": obj.byte_size,
        },
        "m_Name": name,
        "m_NavMeshParams": navmesh_params,
        "tileFormat": {
            "magic": "DNAV",
            "version": NAVMESH_VERSION,
            "headerSize": NAVMESH_TILE_HEADER_SIZE,
            "vertexSize": NAVMESH_VERTEX_SIZE,
            "polygonSize": NAVMESH_POLYGON_SIZE,
            "detailMeshSize": NAVMESH_DETAIL_MESH_SIZE,
            "detailVertexSize": NAVMESH_DETAIL_VERTEX_SIZE,
            "detailTriangleSize": NAVMESH_DETAIL_TRIANGLE_SIZE,
            "bvNodeSize": NAVMESH_BV_NODE_SIZE,
            "vertsPerPolygon": NAVMESH_VERTS_PER_POLYGON,
        },
        "m_NavMeshTiles": tile_documents,
        **trailing,
        "navMeshSettings": settings,
        "binaryFiles": {},
        "summary": {
            "tileCount": len(tile_documents),
            "vertexCount": len(positions) // 3,
            "polygonCount": total_polygons,
            "triangleCount": total_triangles,
            "polygonVertexCounts": {
                str(count): polygon_vertex_counts[count]
                for count in range(3, 7)
                if polygon_vertex_counts[count]
            },
            "areaCounts": dict(sorted(area_counts.items())),
            "flagCounts": dict(sorted(flag_counts.items())),
            "boundsMin": vector_document(bounds_min),
            "boundsMax": vector_document(bounds_max),
            "navMeshSettingsCount": len(settings),
        },
    }
    return NavMeshDataRecord(name, positions, indices, document, bytes(tile_blob_data))

def navmesh_schema_field(name: str) -> TypeTreeNode:
    return next(child for child in load_schema(NAVMESH_DATA_SCHEMA).children if child.name == name)

def parse_navmesh_tile(data: bytes, tile_index: int, include_detail: bool = False) -> dict:
    if len(data) < NAVMESH_TILE_HEADER_SIZE:
        raise NavMeshExportError(f"NavMesh tile {tile_index} is too small")
    header = struct.unpack_from("<11i7f", data, 0)
    magic, version, x, y, layer = header[:5]
    polygon_count, vertex_count = header[5:7]
    detail_mesh_count, detail_vertex_count = header[7:9]
    detail_triangle_count, bv_node_count = header[9:11]
    bounds_min = header[11:14]
    bounds_max = header[14:17]
    bv_quant_factor = header[17]
    if magic != NAVMESH_MAGIC:
        raise NavMeshExportError(f"NavMesh tile {tile_index} has invalid magic 0x{magic:08x}")
    if version != NAVMESH_VERSION:
        raise NavMeshExportError(f"NavMesh tile {tile_index} has unsupported version {version}")
    counts = (
        polygon_count,
        vertex_count,
        detail_mesh_count,
        detail_vertex_count,
        detail_triangle_count,
        bv_node_count,
    )
    if min(counts) < 0:
        raise NavMeshExportError(f"NavMesh tile {tile_index} has a negative count")

    vertex_offset = NAVMESH_TILE_HEADER_SIZE
    polygon_offset = vertex_offset + vertex_count * NAVMESH_VERTEX_SIZE
    detail_mesh_offset = polygon_offset + polygon_count * NAVMESH_POLYGON_SIZE
    detail_vertex_offset = detail_mesh_offset + detail_mesh_count * NAVMESH_DETAIL_MESH_SIZE
    detail_triangle_offset = (
        detail_vertex_offset + detail_vertex_count * NAVMESH_DETAIL_VERTEX_SIZE
    )
    bv_node_offset = (
        detail_triangle_offset + detail_triangle_count * NAVMESH_DETAIL_TRIANGLE_SIZE
    )
    described_size = bv_node_offset + bv_node_count * NAVMESH_BV_NODE_SIZE
    if described_size != len(data):
        raise NavMeshExportError(
            f"NavMesh tile {tile_index} header describes {described_size} bytes "
            f"but the payload is {len(data)} bytes"
        )

    vertices = [
        struct.unpack_from("<3f", data, vertex_offset + index * NAVMESH_VERTEX_SIZE)
        for index in range(vertex_count)
    ]
    polygons: list[dict] = []
    triangle_count = 0
    for polygon_index in range(polygon_count):
        offset = polygon_offset + polygon_index * NAVMESH_POLYGON_SIZE
        vertex_indices = struct.unpack_from("<6H", data, offset)
        neighbors = struct.unpack_from("<6H", data, offset + 12)
        flags = struct.unpack_from("<H", data, offset + 24)[0]
        area = data[offset + 26]
        polygon_type = data[offset + 27]
        polygon_vertex_count = struct.unpack_from("<i", data, offset + 28)[0]
        if not 3 <= polygon_vertex_count <= NAVMESH_VERTS_PER_POLYGON:
            raise NavMeshExportError(
                f"NavMesh tile {tile_index} polygon {polygon_index} has "
                f"invalid vertex count {polygon_vertex_count}"
            )
        selected_vertices = list(vertex_indices[:polygon_vertex_count])
        if any(index >= vertex_count for index in selected_vertices):
            raise NavMeshExportError(
                f"NavMesh tile {tile_index} polygon {polygon_index} has an invalid vertex index"
            )
        triangle_count += polygon_vertex_count - 2
        polygons.append(
            {
                "vertices": selected_vertices,
                "neighbors": list(neighbors[:polygon_vertex_count]),
                "flags": flags,
                "area": area,
                "type": polygon_type,
                "vertexCount": polygon_vertex_count,
            }
        )

    tile = {
        "x": x,
        "y": y,
        "layer": layer,
        "polygonCount": polygon_count,
        "vertexCount": vertex_count,
        "detailMeshCount": detail_mesh_count,
        "detailVertexCount": detail_vertex_count,
        "detailTriangleCount": detail_triangle_count,
        "bvNodeCount": bv_node_count,
        "boundsMin": bounds_min,
        "boundsMax": bounds_max,
        "bvQuantFactor": bv_quant_factor,
        "vertices": vertices,
        "polygons": polygons,
        "triangleCount": triangle_count,
    }
    if include_detail:
        tile.update(
            parse_navmesh_tile_detail(
                data,
                tile_index,
                detail_mesh_offset,
                detail_mesh_count,
                detail_vertex_offset,
                detail_vertex_count,
                detail_triangle_offset,
                detail_triangle_count,
                bv_node_offset,
                bv_node_count,
            )
        )
    return tile

def parse_navmesh_tile_detail(
    data: bytes,
    tile_index: int,
    detail_mesh_offset: int,
    detail_mesh_count: int,
    detail_vertex_offset: int,
    detail_vertex_count: int,
    detail_triangle_offset: int,
    detail_triangle_count: int,
    bv_node_offset: int,
    bv_node_count: int,
) -> dict:
    detail_mesh_end = detail_mesh_offset + detail_mesh_count * NAVMESH_DETAIL_MESH_SIZE
    detail_meshes = [
        {
            "vertexBase": vertex_base,
            "triangleBase": triangle_base,
            "vertexCount": vertex_count,
            "triangleCount": triangle_count,
        }
        for vertex_base, triangle_base, vertex_count, triangle_count in struct.iter_unpack(
            "<IIHH", data[detail_mesh_offset:detail_mesh_end]
        )
    ]
    detail_vertex_end = detail_vertex_offset + detail_vertex_count * NAVMESH_DETAIL_VERTEX_SIZE
    detail_vertices = list(
        struct.iter_unpack("<3f", data[detail_vertex_offset:detail_vertex_end])
    )
    detail_triangle_end = (
        detail_triangle_offset + detail_triangle_count * NAVMESH_DETAIL_TRIANGLE_SIZE
    )
    detail_triangles = list(
        struct.iter_unpack("<4H", data[detail_triangle_offset:detail_triangle_end])
    )
    bv_node_end = bv_node_offset + bv_node_count * NAVMESH_BV_NODE_SIZE
    bv_nodes = [
        {
            "boundsMin": [values[0], values[1], values[2]],
            "boundsMax": [values[3], values[4], values[5]],
            "index": values[6],
        }
        for values in struct.iter_unpack("<6Hi", data[bv_node_offset:bv_node_end])
    ]
    detail_vertex_total = sum(mesh["vertexCount"] for mesh in detail_meshes)
    detail_triangle_total = sum(mesh["triangleCount"] for mesh in detail_meshes)
    if detail_vertex_total != detail_vertex_count:
        raise NavMeshExportError(
            f"NavMesh tile {tile_index} detail meshes claim {detail_vertex_total} "
            f"vertices but the header declares {detail_vertex_count}"
        )
    if detail_triangle_total != detail_triangle_count:
        raise NavMeshExportError(
            f"NavMesh tile {tile_index} detail meshes claim {detail_triangle_total} "
            f"triangles but the header declares {detail_triangle_count}"
        )
    return {
        "detailMeshes": detail_meshes,
        "detailVertices": detail_vertices,
        "detailTriangles": detail_triangles,
        "bvNodes": bv_nodes,
    }

def write_navmesh_sidecars(
    navmesh: NavMeshDataRecord,
    output_root: Path,
    asset: SerializedAsset,
    obj: SerializedObject,
    reserve_path: ReservePath,
) -> NavMeshSidecarResult:
    source_key = f"{asset.relative_path}:{obj.path_id}"
    safe_name = safe_filename(navmesh.name) or "NavMesh"
    json_path = reserve_path(
        output_root
        / "converted"
        / "navmesh_json"
        / "238_NavMeshData"
        / f"{safe_name}__path{obj.path_id}.json",
        f"{source_key}:navmesh-json",
    )
    tile_blob_path = reserve_path(
        json_path.with_name(f"{json_path.stem}.tiles.bin"),
        f"{source_key}:navmesh-tiles",
    )
    json_path.parent.mkdir(parents=True, exist_ok=True)
    tile_blob_path.write_bytes(navmesh.tile_blob_data)
    navmesh.document["binaryFiles"]["navmeshTiles"] = {
        "path": tile_blob_path.name,
        "byteLength": len(navmesh.tile_blob_data),
        "byteOrder": "little-endian",
    }
    json_path.write_text(
        json.dumps(navmesh.document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    metrics = dict(navmesh.document["summary"])
    metrics["binaryFileCount"] = 1
    return NavMeshSidecarResult(json_path, tile_blob_path, navmesh.document, metrics)

def read_count(reader: EndianReader, maximum: int, label: str) -> int:
    count = reader.i32()
    if count < 0 or count > maximum:
        raise NavMeshExportError(f"invalid {label} count {count} at 0x{reader.tell() - 4:x}")
    return count

def vector_document(values) -> dict:
    return dict(zip(("x", "y", "z"), (float(value) for value in values)))

def safe_filename(value: str) -> str:
    invalid = '<>:"/\\|?*'
    cleaned = "".join("_" if char in invalid or ord(char) < 32 else char for char in value).strip(" .")
    return cleaned[:180]
