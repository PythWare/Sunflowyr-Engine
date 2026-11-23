from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from .animation_glb_import import read_glb, accessor_values
from .navmesh_build import build_navmesh_tile, pack_navmesh_tile, NavMeshBuildError
from .navmesh_export import NAVMESH_VERTS_PER_POLYGON

WELD_TOLERANCE = 1e-3
MINIMUM_POLYGON_AREA = 1e-6

class NavMeshFromGlbError(ValueError):
    pass

def read_glb_surface(glb_path: Path) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    document, binary = read_glb(Path(glb_path).read_bytes())
    positions: list[tuple[float, float, float]] = []
    triangles: list[tuple[int, int, int]] = []
    for mesh in document.get("meshes") or []:
        for primitive in mesh.get("primitives") or []:
            mode = primitive.get("mode", 4)
            if mode != 4:
                continue
            attributes = primitive.get("attributes") or {}
            if "POSITION" not in attributes:
                continue
            base = len(positions)
            for value in accessor_values(document, binary, attributes["POSITION"]):
                positions.append((-float(value[0]), float(value[1]), float(value[2])))
            indices_index = primitive.get("indices")
            if indices_index is None:
                count = len(positions) - base
                order = list(range(count))
            else:
                order = [int(value[0]) for value in accessor_values(document, binary, indices_index)]
            for offset in range(0, len(order) - 2, 3):
                first, second, third = order[offset], order[offset + 1], order[offset + 2]
                triangles.append((base + first, base + third, base + second))
    if not triangles:
        raise NavMeshFromGlbError(
            "the GLB holds no triangles, so there is no walkable surface to rebuild"
        )
    return positions, triangles

def clip_polygon_axis(points, axis: int, limit: float, keep_greater: bool):
    if not points:
        return []
    result = []
    count = len(points)
    for index in range(count):
        current = points[index]
        following = points[(index + 1) % count]
        current_inside = (
            current[axis] >= limit if keep_greater else current[axis] <= limit
        )
        following_inside = (
            following[axis] >= limit if keep_greater else following[axis] <= limit
        )
        if current_inside:
            result.append(current)
        if current_inside != following_inside:
            span = following[axis] - current[axis]
            if abs(span) > 1e-12:
                ratio = (limit - current[axis]) / span
                crossing = tuple(
                    current[a] + (following[a] - current[a]) * ratio for a in range(3)
                )
                crossing = tuple(
                    limit if a == axis else crossing[a] for a in range(3)
                )
                result.append(crossing)
    return result

def clip_polygon_to_tile(points, min_x: float, max_x: float, min_z: float, max_z: float):
    points = clip_polygon_axis(points, 0, min_x, True)
    points = clip_polygon_axis(points, 0, max_x, False)
    points = clip_polygon_axis(points, 2, min_z, True)
    points = clip_polygon_axis(points, 2, max_z, False)
    return points

def polygon_area_xz(points) -> float:
    total = 0.0
    count = len(points)
    for index in range(count):
        current = points[index]
        following = points[(index + 1) % count]
        total += current[0] * following[2] - following[0] * current[2]
    return abs(total) * 0.5

def remove_duplicate_points(points):
    cleaned = []
    for point in points:
        if cleaned and all(abs(point[a] - cleaned[-1][a]) < 1e-9 for a in range(3)):
            continue
        cleaned.append(point)
    if (
        len(cleaned) > 1
        and all(abs(cleaned[0][a] - cleaned[-1][a]) < 1e-9 for a in range(3))
    ):
        cleaned.pop()
    return cleaned

def tile_index_for(value: float, tile_size: float) -> int:
    import math

    return int(math.floor(value / tile_size))

def assign_surface_to_tiles(positions, triangles, tile_size: float) -> dict:
    import math

    tiles: dict[tuple[int, int], list] = {}
    for first, second, third in triangles:
        corners = [positions[first], positions[second], positions[third]]
        low_x = min(point[0] for point in corners)
        high_x = max(point[0] for point in corners)
        low_z = min(point[2] for point in corners)
        high_z = max(point[2] for point in corners)
        first_x = tile_index_for(low_x, tile_size)
        last_x = tile_index_for(high_x, tile_size)
        first_z = tile_index_for(low_z, tile_size)
        last_z = tile_index_for(high_z, tile_size)
        for grid_x in range(first_x, last_x + 1):
            for grid_z in range(first_z, last_z + 1):
                min_x = grid_x * tile_size
                min_z = grid_z * tile_size
                if first_x == last_x and first_z == last_z:
                    fragment = corners
                else:
                    fragment = clip_polygon_to_tile(
                        corners, min_x, min_x + tile_size, min_z, min_z + tile_size
                    )
                    fragment = remove_duplicate_points(fragment)
                if len(fragment) < 3:
                    continue
                if polygon_area_xz(fragment) < MINIMUM_POLYGON_AREA:
                    continue
                tiles.setdefault((grid_x, grid_z), []).append(fragment)
    return tiles

def weld_tile_vertices(fragments):
    import math

    vertices: list[tuple[float, float, float]] = []
    buckets: dict[tuple[int, int, int], list[int]] = {}
    polygons: list[dict] = []

    def index_for(point):
        cell = (
            math.floor(point[0] / WELD_TOLERANCE),
            math.floor(point[1] / WELD_TOLERANCE),
            math.floor(point[2] / WELD_TOLERANCE),
        )
        for step_x in (-1, 0, 1):
            for step_y in (-1, 0, 1):
                for step_z in (-1, 0, 1):
                    neighbour = (cell[0] + step_x, cell[1] + step_y, cell[2] + step_z)
                    for candidate in buckets.get(neighbour, ()):
                        existing = vertices[candidate]
                        if all(
                            abs(existing[axis] - point[axis]) <= WELD_TOLERANCE
                            for axis in range(3)
                        ):
                            return candidate
        index = len(vertices)
        vertices.append((float(point[0]), float(point[1]), float(point[2])))
        buckets.setdefault(cell, []).append(index)
        return index

    for fragment in fragments:
        indices: list[int] = []
        for point in fragment:
            index = index_for(point)
            if indices and indices[-1] == index:
                continue
            indices.append(index)
        while len(indices) > 1 and indices[0] == indices[-1]:
            indices.pop()
        if len(indices) != len(set(indices)):
            continue
        if len(indices) < 3:
            continue
        if polygon_area_xz([vertices[i] for i in indices]) < MINIMUM_POLYGON_AREA:
            continue
        if len(indices) <= NAVMESH_VERTS_PER_POLYGON:
            polygons.append({"vertices": indices, "flags": 1, "area": 0, "type": 0})
        else:
            for corner in range(1, len(indices) - 1):
                fan = [indices[0], indices[corner], indices[corner + 1]]
                if polygon_area_xz([vertices[i] for i in fan]) < MINIMUM_POLYGON_AREA:
                    continue
                polygons.append({"vertices": fan, "flags": 1, "area": 0, "type": 0})
    return vertices, polygons

def rebuild_navmesh_tiles(glb_path: Path, source_document: dict[str, Any]) -> tuple[list[dict], bytes, dict]:
    parameters = source_document.get("m_NavMeshParams") or {}
    tile_size = float(parameters.get("tileSize") or 0.0)
    cell_size = float(parameters.get("cellSize") or 0.0)
    if tile_size <= 0.0 or cell_size <= 0.0:
        raise NavMeshFromGlbError(
            "the source navmesh JSON has no usable m_NavMeshParams tileSize/cellSize"
        )
    quant_factor = 1.0 / cell_size

    positions, triangles = read_glb_surface(glb_path)
    grouped = assign_surface_to_tiles(positions, triangles, tile_size)
    if not grouped:
        raise NavMeshFromGlbError("no triangle survived tile assignment")

    world_low = min(point[1] for point in positions)
    world_high = max(point[1] for point in positions)
    if world_high - world_low < 1.0:
        world_high = world_low + 1.0

    tile_documents: list[dict] = []
    blob = bytearray()
    total_polygons = 0
    total_vertices = 0
    for grid_x, grid_z in sorted(grouped):
        fragments = grouped[(grid_x, grid_z)]
        vertices, polygons = weld_tile_vertices(fragments)
        if not polygons:
            continue
        bounds_min = [grid_x * tile_size, world_low, grid_z * tile_size]
        bounds_max = [
            grid_x * tile_size + tile_size,
            world_high,
            grid_z * tile_size + tile_size,
        ]
        try:
            tile = build_navmesh_tile(
                grid_x=grid_x,
                grid_y=grid_z,
                vertices=vertices,
                polygons=polygons,
                bounds_min=bounds_min,
                bounds_max=bounds_max,
                quant_factor=quant_factor,
            )
            data = pack_navmesh_tile(tile)
        except NavMeshBuildError as error:
            raise NavMeshFromGlbError(
                f"NavMesh tile ({grid_x}, {grid_z}) couldnt be rebuilt: {error}"
            ) from error
        offset = len(blob)
        blob.extend(data)
        total_polygons += len(polygons)
        total_vertices += len(vertices)
        tile_documents.append(
            {
                "tileIndex": len(tile_documents),
                "x": grid_x,
                "y": grid_z,
                "layer": 0,
                "vertexCount": len(vertices),
                "polygonCount": len(polygons),
                "triangleCount": sum(len(p["vertices"]) - 2 for p in polygons),
                "detailMeshCount": len(tile["detailMeshes"]),
                "detailVertexCount": len(tile["detailVertices"]),
                "detailTriangleCount": len(tile["detailTriangles"]),
                "bvNodeCount": len(tile["bvNodes"]),
                "boundsMin": {"x": bounds_min[0], "y": bounds_min[1], "z": bounds_min[2]},
                "boundsMax": {"x": bounds_max[0], "y": bounds_max[1], "z": bounds_max[2]},
                "bvQuantFactor": quant_factor,
                "blob": {
                    "binaryRole": "navmeshTiles",
                    "byteOffset": offset,
                    "byteLength": len(data),
                },
            }
        )

    metrics = {
        "tileCount": len(tile_documents),
        "vertexCount": total_vertices,
        "polygonCount": total_polygons,
        "sourceTriangleCount": len(triangles),
        "byteLength": len(blob),
    }
    return tile_documents, bytes(blob), metrics

def read_navmesh_glb_source(glb_path: Path) -> tuple[str, int] | None:
    document, unused_binary = read_glb(Path(glb_path).read_bytes())
    for collection in ("nodes", "meshes"):
        for item in document.get(collection) or []:
            extras = item.get("extras")
            if not isinstance(extras, dict):
                continue
            container = extras.get("unity_container")
            path_id = extras.get("unity_path_id")
            if container and path_id is not None:
                return str(container), int(path_id)
    return None

def looks_like_navmesh_glb(path: Path) -> bool:
    import struct

    try:
        with Path(path).open("rb") as handle:
            header = handle.read(12)
            if len(header) < 12 or struct.unpack_from("<I", header, 0)[0] != 0x46546C67:
                return False
            length, kind = struct.unpack("<II", handle.read(8))
            if kind != 0x4E4F534A:
                return False
            chunk = handle.read(min(length, 4_000_000))
    except (OSError, struct.error):
        return False
    return b"unity_navmesh_tile_count" in chunk

def rebuild_navmesh_folder(glb_path: Path, out_folder: Path, data_root: Path) -> dict:
    from .model_context import build_model_context
    from .navmesh_export import read_navmesh_data
    from .serialized import load_serialized_asset

    glb_path = Path(glb_path)
    out_folder = Path(out_folder)
    data_root = Path(data_root)

    source = read_navmesh_glb_source(glb_path)
    if source is None:
        raise NavMeshFromGlbError(
            f"{glb_path.name} has no Sunflowyr navmesh data, use a navmesh GLB from the unpack"
        )
    container, path_id = source
    asset_path = data_root / container
    if not asset_path.is_file():
        raise NavMeshFromGlbError(f"the game file {container} wasnt found under {data_root}")

    asset = load_serialized_asset(asset_path, data_root)
    target = next((obj for obj in asset.objects if obj.path_id == path_id), None)
    if target is None:
        raise NavMeshFromGlbError(f"{container} has no object with path id {path_id}")
    record = read_navmesh_data(build_model_context([asset]), asset, target)
    source_document = record.document

    tile_documents, blob, metrics = rebuild_navmesh_tiles(glb_path, source_document)

    out_folder.mkdir(parents=True, exist_ok=True)
    stem = f"{record.name or 'NavMesh'}__path{path_id}"
    json_path = out_folder / f"{stem}.json"
    blob_path = out_folder / f"{stem}.tiles.bin"
    blob_path.write_bytes(blob)

    document = dict(source_document)
    document["m_NavMeshTiles"] = tile_documents
    document["binaryFiles"] = {
        "navmeshTiles": {
            "path": blob_path.name,
            "byteLength": len(blob),
            "byteOrder": "little-endian",
        }
    }
    summary = dict(source_document.get("summary") or {})
    summary.update(
        {
            "tileCount": metrics["tileCount"],
            "vertexCount": metrics["vertexCount"],
            "polygonCount": metrics["polygonCount"],
            "triangleCount": sum(tile["triangleCount"] for tile in tile_documents),
        }
    )
    document["summary"] = summary
    json_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    catalog = {
        "sunflowyrTaildata": 1,
        "files": {
            json_path.name: {
                "sunflowyrTaildata": 1,
                "kind": "navmesh_data_json",
                "unityVersion": asset.unity_version,
                "serializedFormatVersion": asset.format_version,
                "container": container,
                "appendTarget": container,
                "pathId": path_id,
                "classId": target.class_id,
                "className": target.type_name,
                "name": record.name,
                "byteSize": target.byte_size,
            }
        },
    }
    catalog_path = out_folder / "sunflowyr_taildata.json"
    catalog_path.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    metrics.update(
        {
            "container": container,
            "pathId": path_id,
            "name": record.name,
            "folder": str(out_folder),
            "jsonPath": str(json_path),
            "tilePath": str(blob_path),
            "originalTileCount": len(source_document.get("m_NavMeshTiles") or []),
            "originalPolygonCount": (source_document.get("summary") or {}).get("polygonCount", 0),
        }
    )
    return metrics
