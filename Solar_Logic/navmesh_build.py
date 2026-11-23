from __future__ import annotations
import math, struct
from .navmesh_export import (
    NAVMESH_BV_NODE_SIZE,
    NAVMESH_DETAIL_MESH_SIZE,
    NAVMESH_DETAIL_TRIANGLE_SIZE,
    NAVMESH_DETAIL_VERTEX_SIZE,
    NAVMESH_MAGIC,
    NAVMESH_POLYGON_SIZE,
    NAVMESH_TILE_HEADER_SIZE,
    NAVMESH_VERSION,
    NAVMESH_VERTEX_SIZE,
    NAVMESH_VERTS_PER_POLYGON,
)

DT_EXT_LINK = 0x8000
PORTAL_X_POSITIVE = 0
PORTAL_Z_POSITIVE = 2
PORTAL_X_NEGATIVE = 4
PORTAL_Z_NEGATIVE = 6
BOUNDARY_EPSILON = 1e-3
MAX_TILE_VERTICES = 0xFFFF

class NavMeshBuildError(ValueError):
    pass

def pack_navmesh_tile(tile: dict) -> bytes:
    vertices = tile["vertices"]
    polygons = tile["polygons"]
    detail_meshes = tile["detailMeshes"]
    detail_vertices = tile["detailVertices"]
    detail_triangles = tile["detailTriangles"]
    bv_nodes = tile["bvNodes"]

    if len(vertices) > MAX_TILE_VERTICES:
        raise NavMeshBuildError(
            f"NavMesh tile holds {len(vertices)} vertices but polygon indices are "
            f"u16 so a tile cant exceed {MAX_TILE_VERTICES}"
        )
    if len(detail_meshes) != len(polygons):
        raise NavMeshBuildError(
            f"NavMesh tile has {len(polygons)} polygons but {len(detail_meshes)} "
            "detail meshes, Unity requires one detail mesh per polygon"
        )

    out = bytearray()
    out += struct.pack(
        "<11i7f",
        NAVMESH_MAGIC,
        NAVMESH_VERSION,
        int(tile["x"]),
        int(tile["y"]),
        int(tile["layer"]),
        len(polygons),
        len(vertices),
        len(detail_meshes),
        len(detail_vertices),
        len(detail_triangles),
        len(bv_nodes),
        *(float(value) for value in tile["boundsMin"]),
        *(float(value) for value in tile["boundsMax"]),
        float(tile["bvQuantFactor"]),
    )
    for vertex in vertices:
        out += struct.pack("<3f", *vertex)
    for index, polygon in enumerate(polygons):
        vertex_indices = list(polygon["vertices"])
        neighbors = list(polygon["neighbors"])
        count = len(vertex_indices)
        if not 3 <= count <= NAVMESH_VERTS_PER_POLYGON:
            raise NavMeshBuildError(
                f"NavMesh polygon {index} has {count} vertices, Detour allows 3 to "
                f"{NAVMESH_VERTS_PER_POLYGON}"
            )
        if len(neighbors) != count:
            raise NavMeshBuildError(
                f"NavMesh polygon {index} has {count} vertices but "
                f"{len(neighbors)} neighbour slots"
            )
        padding = [0] * (NAVMESH_VERTS_PER_POLYGON - count)
        out += struct.pack("<6H", *(vertex_indices + padding))
        out += struct.pack("<6H", *(neighbors + padding))
        out += struct.pack("<H", int(polygon["flags"]))
        out += bytes((int(polygon["area"]) & 0xFF, int(polygon["type"]) & 0xFF))
        out += struct.pack("<i", count)
    for mesh in detail_meshes:
        out += struct.pack(
            "<IIHH",
            int(mesh["vertexBase"]),
            int(mesh["triangleBase"]),
            int(mesh["vertexCount"]),
            int(mesh["triangleCount"]),
        )
    for vertex in detail_vertices:
        out += struct.pack("<3f", *vertex)
    for triangle in detail_triangles:
        out += struct.pack("<4H", *triangle)
    for node in bv_nodes:
        out += struct.pack(
            "<6Hi", *node["boundsMin"], *node["boundsMax"], int(node["index"])
        )

    expected = (
        NAVMESH_TILE_HEADER_SIZE
        + len(vertices) * NAVMESH_VERTEX_SIZE
        + len(polygons) * NAVMESH_POLYGON_SIZE
        + len(detail_meshes) * NAVMESH_DETAIL_MESH_SIZE
        + len(detail_vertices) * NAVMESH_DETAIL_VERTEX_SIZE
        + len(detail_triangles) * NAVMESH_DETAIL_TRIANGLE_SIZE
        + len(bv_nodes) * NAVMESH_BV_NODE_SIZE
    )
    if len(out) != expected:
        raise NavMeshBuildError(
            f"NavMesh tile writer produced {len(out)} bytes but the header "
            f"describes {expected}"
        )
    return bytes(out)

def compute_polygon_neighbors(
    polygons: list[dict],
    vertices: list,
    bounds_min,
    bounds_max,
) -> int:
    edge_owner: dict[tuple[int, int], int] = {}
    conflicts = 0
    for polygon_index, polygon in enumerate(polygons):
        indices = polygon["vertices"]
        count = len(indices)
        for slot in range(count):
            edge = (indices[slot], indices[(slot + 1) % count])
            if edge in edge_owner:
                conflicts += 1
                continue
            edge_owner[edge] = polygon_index

    for polygon_index, polygon in enumerate(polygons):
        indices = polygon["vertices"]
        count = len(indices)
        neighbors = [0] * count
        for slot in range(count):
            start = indices[slot]
            end = indices[(slot + 1) % count]
            match = edge_owner.get((end, start))
            if match is not None and match != polygon_index:
                neighbors[slot] = match + 1
                continue
            side = classify_boundary_edge(
                vertices[start], vertices[end], bounds_min, bounds_max
            )
            neighbors[slot] = DT_EXT_LINK | side if side is not None else 0
        polygon["neighbors"] = neighbors
    return conflicts

def classify_boundary_edge(start, end, bounds_min, bounds_max):
    if (
        abs(start[0] - bounds_max[0]) <= BOUNDARY_EPSILON
        and abs(end[0] - bounds_max[0]) <= BOUNDARY_EPSILON
    ):
        return PORTAL_X_POSITIVE
    if (
        abs(start[2] - bounds_max[2]) <= BOUNDARY_EPSILON
        and abs(end[2] - bounds_max[2]) <= BOUNDARY_EPSILON
    ):
        return PORTAL_Z_POSITIVE
    if (
        abs(start[0] - bounds_min[0]) <= BOUNDARY_EPSILON
        and abs(end[0] - bounds_min[0]) <= BOUNDARY_EPSILON
    ):
        return PORTAL_X_NEGATIVE
    if (
        abs(start[2] - bounds_min[2]) <= BOUNDARY_EPSILON
        and abs(end[2] - bounds_min[2]) <= BOUNDARY_EPSILON
    ):
        return PORTAL_Z_NEGATIVE
    return None

def build_degenerate_detail_mesh(polygons: list[dict]) -> tuple[list[dict], list, list]:
    detail_meshes: list[dict] = []
    detail_triangles: list = []
    triangle_base = 0
    for polygon in polygons:
        count = len(polygon["vertices"])
        detail_meshes.append(
            {
                "vertexBase": 0,
                "triangleBase": triangle_base,
                "vertexCount": 0,
                "triangleCount": count - 2,
            }
        )
        for corner in range(2, count):
            detail_triangles.append((0, corner - 1, corner, detail_edge_flags(corner, count)))
        triangle_base += count - 2
    return detail_meshes, [], detail_triangles

def detail_edge_flags(corner: int, count: int) -> int:
    flags = 0
    if corner == 2:
        flags |= 1 << 4
    if corner == count - 1:
        flags |= 1 << 2
    flags |= 1 << 0
    return flags

def clamp(value: int, low: int, high: int) -> int:
    return low if value < low else (high if value > high else value)

def quantize_polygon_bounds(points, origin, quant_factor):
    low = [min(point[axis] for point in points) for axis in range(3)]
    high = [max(point[axis] for point in points) for axis in range(3)]
    quantized_min = [0, 0, 0]
    quantized_max = [0, 0, 0]
    for axis in (0, 2):
        quantized_min[axis] = clamp(
            int(math.floor((low[axis] - origin[axis]) * quant_factor)), 0, 0xFFFF
        )
        quantized_max[axis] = clamp(
            int(math.ceil((high[axis] - origin[axis]) * quant_factor)), 0, 0xFFFF
        )
    quantized_min[1] = clamp(
        int(math.floor(low[1] - origin[1]) * quant_factor), 0, 0xFFFF
    )
    quantized_max[1] = clamp(
        int(math.ceil(high[1] - origin[1]) * quant_factor), 0, 0xFFFF
    )
    return quantized_min, quantized_max

def longest_axis(x: int, y: int, z: int) -> int:
    axis = 0
    largest = x
    if y > largest:
        axis = 1
        largest = y
    if z > largest:
        axis = 2
    return axis

def calc_extends(items, first, last):
    bounds_min = list(items[first][0])
    bounds_max = list(items[first][1])
    for index in range(first + 1, last):
        item_min, item_max = items[index][0], items[index][1]
        for axis in range(3):
            if item_min[axis] < bounds_min[axis]:
                bounds_min[axis] = item_min[axis]
            if item_max[axis] > bounds_max[axis]:
                bounds_max[axis] = item_max[axis]
    return bounds_min, bounds_max

def subdivide(items, first, last, nodes):
    count = last - first
    current = len(nodes)
    node = {"boundsMin": None, "boundsMax": None, "index": 0}
    nodes.append(node)
    if count == 1:
        node["boundsMin"] = list(items[first][0])
        node["boundsMax"] = list(items[first][1])
        node["index"] = items[first][2]
        return
    bounds_min, bounds_max = calc_extends(items, first, last)
    node["boundsMin"] = bounds_min
    node["boundsMax"] = bounds_max
    axis = longest_axis(
        bounds_max[0] - bounds_min[0],
        bounds_max[1] - bounds_min[1],
        bounds_max[2] - bounds_min[2],
    )
    chunk = items[first:last]
    chunk.sort(key=lambda item: item[0][axis])
    items[first:last] = chunk
    split = first + count // 2
    subdivide(items, first, split, nodes)
    subdivide(items, split, last, nodes)
    node["index"] = -(len(nodes) - current)

def build_bv_tree(
    polygons: list[dict],
    vertices: list,
    detail_meshes: list[dict],
    detail_vertices: list,
    origin,
    quant_factor: float,
) -> list[dict]:
    items = []
    for index, polygon in enumerate(polygons):
        points = [vertices[i] for i in polygon["vertices"]]
        mesh = detail_meshes[index]
        base = mesh["vertexBase"]
        for offset in range(mesh["vertexCount"]):
            points.append(detail_vertices[base + offset])
        quantized_min, quantized_max = quantize_polygon_bounds(points, origin, quant_factor)
        items.append((quantized_min, quantized_max, index))
    nodes: list[dict] = []
    if items:
        subdivide(items, 0, len(items), nodes)
    while len(nodes) < 2 * len(polygons):
        nodes.append({"boundsMin": [0, 0, 0], "boundsMax": [0, 0, 0], "index": 0})
    return nodes

def build_navmesh_tile(
    grid_x: int,
    grid_y: int,
    vertices: list,
    polygons: list[dict],
    bounds_min,
    bounds_max,
    quant_factor: float,
    layer: int = 0,
) -> dict:
    if len(vertices) > MAX_TILE_VERTICES:
        raise NavMeshBuildError(
            f"NavMesh tile holds {len(vertices)} vertices, over the u16 limit of "
            f"{MAX_TILE_VERTICES}. split the geometry across more tiles"
        )
    for polygon in polygons:
        polygon.setdefault("flags", 1)
        polygon.setdefault("area", 0)
        polygon.setdefault("type", 0)
    conflicts = compute_polygon_neighbors(polygons, vertices, bounds_min, bounds_max)
    detail_meshes, detail_vertices, detail_triangles = build_degenerate_detail_mesh(polygons)
    bv_nodes = build_bv_tree(
        polygons, vertices, detail_meshes, detail_vertices, bounds_min, quant_factor
    )
    return {
        "buildStats": {"unpairedEdges": conflicts},
        "x": grid_x,
        "y": grid_y,
        "layer": layer,
        "boundsMin": list(bounds_min),
        "boundsMax": list(bounds_max),
        "bvQuantFactor": quant_factor,
        "vertices": vertices,
        "polygons": polygons,
        "detailMeshes": detail_meshes,
        "detailVertices": detail_vertices,
        "detailTriangles": detail_triangles,
        "bvNodes": bv_nodes,
    }
