from __future__ import annotations
import json, math, struct, sys
from array import array
from pathlib import Path
from typing import Callable, Iterable
from .mesh_convert import MeshData
from .model_context import ObjectKey, SceneMaterial, SceneTexture

GL_FLOAT = 5126
GL_UNSIGNED_SHORT = 5123
GL_UNSIGNED_INT = 5125
GL_ARRAY_BUFFER = 34962
GL_ELEMENT_ARRAY_BUFFER = 34963
EXACT_NORMAL = "_UNITY_NORMAL_LENGTH"
EXACT_TANGENT = ("_UNITY_TANGENT_X", "_UNITY_TANGENT_Y", "_UNITY_TANGENT_Z", "_UNITY_TANGENT_W")

Matrix = tuple[tuple[float, ...], ...]
TextureLoader = Callable[[SceneTexture], bytes | None]

class GlbBuilder:
    def __init__(self, name: str, taildata: dict | None = None) -> None:
        self.document: dict = {
            "asset": {"version": "2.0", "generator": "Sunflowyr Engine"},
            "scene": 0,
            "scenes": [{"name": name, "nodes": []}],
            "nodes": [],
            "skins": [],
            "meshes": [],
            "materials": [],
            "textures": [],
            "images": [],
            "accessors": [],
            "bufferViews": [],
        }
        if taildata is not None:
            self.document["extras"] = {"sunflowyrTaildata": taildata}
        self.binary = bytearray()
        self.material_cache: dict[object, int] = {}
        self.texture_cache: dict[ObjectKey, int] = {}
        self.mesh_cache: dict[object, int] = {}

    def add_node(
        self,
        name: str,
        *,
        translation: tuple[float, float, float] | None = None,
        rotation: tuple[float, float, float, float] | None = None,
        scale: tuple[float, float, float] | None = None,
        mesh: int | None = None,
        skin: int | None = None,
        extras: dict | None = None,
    ) -> int:
        node: dict = {"name": name}
        if translation is not None and not near_tuple(translation, (0.0, 0.0, 0.0)):
            node["translation"] = list(translation)
        if rotation is not None and not near_tuple(rotation, (0.0, 0.0, 0.0, 1.0)):
            node["rotation"] = list(rotation)
        if scale is not None and not near_tuple(scale, (1.0, 1.0, 1.0)):
            node["scale"] = list(scale)
        if mesh is not None:
            node["mesh"] = mesh
        if skin is not None:
            node["skin"] = skin
        if extras:
            node["extras"] = extras
        index = len(self.document["nodes"])
        self.document["nodes"].append(node)
        return index

    def set_node_children(self, node_index: int, children: list[int]) -> None:
        if children:
            self.document["nodes"][node_index]["children"] = children

    def set_scene_roots(self, roots: list[int]) -> None:
        self.document["scenes"][0]["nodes"] = roots

    def add_animation(
        self,
        name: str,
        tracks: list[dict],
        *,
        extras: dict | None = None,
    ) -> int | None:
        samplers: list[dict] = []
        channels: list[dict] = []
        for track in tracks:
            times = track.get("times")
            values = track.get("values")
            node_index = track.get("node")
            target_path = track.get("path")
            value_type = track.get("value_type")
            if (
                not isinstance(times, list)
                or not isinstance(values, list)
                or not times
                or len(times) != len(values)
                or not isinstance(node_index, int)
                or target_path not in {"translation", "rotation", "scale", "weights"}
                or value_type not in {"SCALAR", "VEC3", "VEC4"}
            ):
                continue
            flat_values = array("f")
            for value in values:
                if isinstance(value, (list, tuple)):
                    flat_values.extend(float(item) for item in value)
                else:
                    flat_values.append(float(value))
            time_values = array("f", (float(value) for value in times))
            input_accessor = self.add_accessor(
                time_values,
                GL_FLOAT,
                len(times),
                "SCALAR",
                minimum=[min(times)],
                maximum=[max(times)],
            )
            output_accessor = self.add_accessor(
                flat_values,
                GL_FLOAT,
                len(values),
                value_type,
            )
            sampler_index = len(samplers)
            samplers.append(
                {
                    "input": input_accessor,
                    "output": output_accessor,
                    "interpolation": str(track.get("interpolation") or "LINEAR"),
                }
            )
            channels.append(
                {
                    "sampler": sampler_index,
                    "target": {"node": node_index, "path": target_path},
                }
            )
        if not channels:
            return None
        animation: dict = {"name": name, "samplers": samplers, "channels": channels}
        if extras:
            animation["extras"] = extras
        animations = self.document.setdefault("animations", [])
        index = len(animations)
        animations.append(animation)
        return index

    def add_animation_rig_marker(
        self,
        name: str,
        joints: list[int],
        skeleton: int,
    ) -> int | None:
        if not joints or skeleton not in joints:
            return None
        skin_index = len(self.document["skins"])
        self.document["skins"].append(
            {
                "name": name,
                "joints": joints,
                "skeleton": skeleton,
            }
        )
        positions = array("f", (0.0, 0.0, 0.0, 0.001, 0.0, 0.0, 0.0, 0.001, 0.0))
        joint_values = array("H", (0, 0, 0, 0) * 3)
        weights = array("f", (1.0, 0.0, 0.0, 0.0) * 3)
        indices = array("I", (0, 1, 2))
        primitive = {
            "attributes": {
                "POSITION": self.add_accessor(
                    positions,
                    GL_FLOAT,
                    3,
                    "VEC3",
                    target=GL_ARRAY_BUFFER,
                    minimum=[0.0, 0.0, 0.0],
                    maximum=[0.001, 0.001, 0.0],
                ),
                "JOINTS_0": self.add_accessor(
                    joint_values,
                    GL_UNSIGNED_SHORT,
                    3,
                    "VEC4",
                    target=GL_ARRAY_BUFFER,
                ),
                "WEIGHTS_0": self.add_accessor(
                    weights,
                    GL_FLOAT,
                    3,
                    "VEC4",
                    target=GL_ARRAY_BUFFER,
                ),
            },
            "indices": self.add_accessor(
                indices,
                GL_UNSIGNED_INT,
                3,
                "SCALAR",
                target=GL_ELEMENT_ARRAY_BUFFER,
            ),
            "mode": 4,
        }
        mesh_index = len(self.document["meshes"])
        self.document["meshes"].append(
            {
                "name": f"{name} Marker",
                "primitives": [primitive],
                "extras": {"sunflowyr_animation_rig_marker": True},
            }
        )
        return self.add_node(
            f"{name} Rig",
            mesh=mesh_index,
            skin=skin_index,
            extras={"sunflowyr_animation_rig_marker": True},
        )

    def add_material(self, material: SceneMaterial, texture_loader: TextureLoader | None = None) -> int:
        cache_key: object = material.key if material.key is not None else ("default", material.name)
        cached = self.material_cache.get(cache_key)
        if cached is not None:
            return cached

        diffuse = [float(value) for value in material.diffuse]
        pbr: dict = {
            "baseColorFactor": diffuse,
            "metallicFactor": 0.0,
            "roughnessFactor": 1.0,
        }
        output: dict = {
            "name": material.name,
            "pbrMetallicRoughness": pbr,
            "doubleSided": True,
        }
        if material.alpha_mode == "BLEND":
            output["alphaMode"] = "BLEND"
        elif material.alpha_mode == "MASK":
            output["alphaMode"] = "MASK"
            output["alphaCutoff"] = 0.5
        if material.key is not None:
            output["extras"] = key_extras(material.key, "material")

        main_texture, normal_texture = select_material_textures(material)
        can_link = texture_loader is not None or getattr(self, "texture_uri_resolver", None) is not None
        if can_link and main_texture is not None:
            texture_index = self.add_texture(main_texture, texture_loader)
            if texture_index is not None:
                pbr["baseColorTexture"] = {"index": texture_index}
        if can_link and normal_texture is not None:
            texture_index = self.add_texture(normal_texture, texture_loader)
            if texture_index is not None:
                output["normalTexture"] = {"index": texture_index}

        index = len(self.document["materials"])
        self.document["materials"].append(output)
        self.material_cache[cache_key] = index
        return index

    def add_texture(self, texture: SceneTexture, texture_loader: TextureLoader | None) -> int | None:
        cached = self.texture_cache.get(texture.key)
        if cached is not None:
            return cached
        resolver = getattr(self, "texture_uri_resolver", None)
        image: dict[str, object] | None = None
        if resolver is not None:
            uri = resolver(texture)
            if uri:
                image = {
                    "name": texture.name,
                    "uri": uri,
                    "mimeType": "image/png",
                    "extras": key_extras(texture.key, "texture"),
                }
        if image is None:
            if texture_loader is None:
                return None
            png = texture_loader(texture)
            if png is None:
                return None
            view = self.add_buffer_view(png)
            image = {
                "name": texture.name,
                "bufferView": view,
                "mimeType": "image/png",
                "extras": key_extras(texture.key, "texture"),
            }
        image_index = len(self.document["images"])
        self.document["images"].append(image)
        texture_index = len(self.document["textures"])
        self.document["textures"].append({"source": image_index})
        self.texture_cache[texture.key] = texture_index
        return texture_index

    def add_mesh(
        self,
        mesh: MeshData,
        selected_submeshes: list[int],
        materials: list[SceneMaterial],
        texture_loader: TextureLoader | None,
        *,
        source_extras: dict | None = None,
        position_matrix: Matrix | None = None,
        normal_matrix: Matrix | None = None,
        tangent_matrix: Matrix | None = None,
        cache_key: object | None = None,
        skinned: bool = False,
    ) -> int | None:
        if cache_key is not None:
            cached = self.mesh_cache.get(cache_key)
            if cached is not None:
                return cached

        primitives: list[dict] = []
        exact_normals = normal_matrix is None and len(mesh.normals) == len(mesh.vertices) and any(abs(vector_length(normal) - 1.0) > 1e-5 for normal in mesh.normals)
        for slot, submesh_index in enumerate(selected_submeshes):
            if not (0 <= submesh_index < len(mesh.submesh_indices)):
                continue
            source_indices = mesh.submesh_indices[submesh_index]
            if not source_indices:
                continue
            material = materials[slot] if slot < len(materials) else materials[-1]
            material_index = self.add_material(material, texture_loader)
            primitive = self.add_mesh_primitive(
                mesh,
                source_indices,
                material_index,
                position_matrix,
                normal_matrix,
                tangent_matrix,
                skinned,
                exact_normals,
            )
            if primitive is not None:
                primitives.append(primitive)

        if not primitives:
            return None
        output: dict = {"name": mesh.name or "Mesh", "primitives": primitives}
        if source_extras:
            output["extras"] = source_extras
        index = len(self.document["meshes"])
        self.document["meshes"].append(output)
        if cache_key is not None:
            self.mesh_cache[cache_key] = index
        return index

    def add_raw_mesh(
        self,
        name: str,
        positions: array,
        indices: array,
        *,
        uv0: array | None = None,
        material: SceneMaterial | None = None,
        extras: dict | None = None,
    ) -> int:
        position_count = len(positions) // 3
        position_values = list(vector_min_max(positions, 3))
        attributes = {
            "POSITION": self.add_accessor(
                positions,
                GL_FLOAT,
                position_count,
                "VEC3",
                target=GL_ARRAY_BUFFER,
                minimum=position_values[0],
                maximum=position_values[1],
            )
        }
        if uv0 is not None and len(uv0) == position_count * 2:
            attributes["TEXCOORD_0"] = self.add_accessor(
                uv0,
                GL_FLOAT,
                position_count,
                "VEC2",
                target=GL_ARRAY_BUFFER,
            )
        primitive: dict = {
            "attributes": attributes,
            "indices": self.add_accessor(
                indices,
                GL_UNSIGNED_INT,
                len(indices),
                "SCALAR",
                target=GL_ELEMENT_ARRAY_BUFFER,
            ),
            "mode": 4,
        }
        if material is not None:
            primitive["material"] = self.add_material(material)
        mesh: dict = {"name": name, "primitives": [primitive]}
        if extras:
            mesh["extras"] = extras
        index = len(self.document["meshes"])
        self.document["meshes"].append(mesh)
        return index

    def add_mesh_primitive(
        self,
        mesh: MeshData,
        source_indices: list[int],
        material_index: int,
        position_matrix: Matrix | None,
        normal_matrix: Matrix | None,
        tangent_matrix: Matrix | None,
        skinned: bool = False,
        exact_normals: bool = False,
    ) -> dict | None:
        used = sorted(set(source_indices))
        if not used or used[-1] >= len(mesh.vertices):
            return None
        remap = {source: index for index, source in enumerate(used)}
        positions = array("f")
        normals = array("f")
        tangents = array("f")
        colors = array("f")
        uv_layers = [array("f") for _ in mesh.uv_layers]

        has_normals = len(mesh.normals) == len(mesh.vertices)
        has_tangents = len(mesh.tangents) == len(mesh.vertices)
        has_colors = len(mesh.colors) == len(mesh.vertices)
        valid_uv = [len(layer) == len(mesh.vertices) for layer in mesh.uv_layers]
        normal_lengths = array("f")
        tangent_parts = [array("f") for unused in EXACT_TANGENT]
        uv_more = [(index, extra, [array("f") for unused in extra[0]]) for index, extra in enumerate(mesh.uv_extra) if len(extra) == len(mesh.vertices) and extra and extra[0]]

        for source_index in used:
            position = mesh.vertices[source_index]
            if position_matrix is not None:
                position = transform_point(position_matrix, position)
            positions.extend((-position[0], position[1], position[2]))

            if has_normals:
                normal = mesh.normals[source_index]
                if normal_matrix is not None:
                    normal = transform_vector(normal_matrix, normal)
                normal_lengths.append(vector_length(normal))
                unit = normalize3(normal)
                normals.extend((-unit[0], unit[1], unit[2]))

            if has_tangents:
                tangent = mesh.tangents[source_index]
                tangent_xyz = tangent[:3]
                if tangent_matrix is not None:
                    tangent_xyz = transform_vector(tangent_matrix, tangent_xyz)
                for part, value in zip(tangent_parts, (-tangent_xyz[0], tangent_xyz[1], tangent_xyz[2], -tangent[3])):
                    part.append(value)
                unit = normalize3(tangent_xyz)
                tangents.extend((-unit[0], unit[1], unit[2], -tangent[3]))

            if has_colors:
                colors.extend(mesh.colors[source_index])
            for uv_index, layer in enumerate(mesh.uv_layers):
                if valid_uv[uv_index]:
                    uv_layers[uv_index].extend(layer[source_index])
            for unused, extra, parts in uv_more:
                for part, value in zip(parts, extra[source_index]):
                    part.append(value)

        indices = array("I")
        for index in range(0, len(source_indices), 3):
            triangle = source_indices[index : index + 3]
            if len(triangle) != 3:
                continue
            indices.extend((remap[triangle[2]], remap[triangle[1]], remap[triangle[0]]))

        minimum, maximum = vector_min_max(positions, 3)
        attributes = {
            "POSITION": self.add_accessor(
                positions,
                GL_FLOAT,
                len(used),
                "VEC3",
                target=GL_ARRAY_BUFFER,
                minimum=minimum,
                maximum=maximum,
            )
        }
        if has_normals:
            attributes["NORMAL"] = self.add_accessor(normals, GL_FLOAT, len(used), "VEC3", target=GL_ARRAY_BUFFER)
            if exact_normals:
                attributes[EXACT_NORMAL] = self.add_accessor(normal_lengths, GL_FLOAT, len(used), "SCALAR", target=GL_ARRAY_BUFFER)
        if has_tangents:
            attributes["TANGENT"] = self.add_accessor(tangents, GL_FLOAT, len(used), "VEC4", target=GL_ARRAY_BUFFER)
            for name, part in zip(EXACT_TANGENT, tangent_parts):
                attributes[name] = self.add_accessor(part, GL_FLOAT, len(used), "SCALAR", target=GL_ARRAY_BUFFER)
        if has_colors:
            attributes["COLOR_0"] = self.add_accessor(colors, GL_FLOAT, len(used), "VEC4", target=GL_ARRAY_BUFFER)
        for uv_index, values in enumerate(uv_layers):
            if valid_uv[uv_index]:
                attributes[f"TEXCOORD_{uv_index}"] = self.add_accessor(
                    values,
                    GL_FLOAT,
                    len(used),
                    "VEC2",
                    target=GL_ARRAY_BUFFER,
                )
        for uv_index, unused, parts in uv_more:
            for name, part in zip(uv_extra_names(uv_index), parts):
                attributes[name] = self.add_accessor(part, GL_FLOAT, len(used), "SCALAR", target=GL_ARRAY_BUFFER)
        if skinned and len(mesh.skin) == len(mesh.vertices):
            joints = array("H")
            weights = array("f")
            for source_index in used:
                bone_weight = mesh.skin[source_index]
                joints.extend(max(0, min(65535, int(value))) for value in bone_weight.indices)
                total = sum(max(0.0, float(value)) for value in bone_weight.weights)
                if total <= 0.0:
                    weights.extend((1.0, 0.0, 0.0, 0.0))
                else:
                    weights.extend(max(0.0, float(value)) / total for value in bone_weight.weights)
            attributes["JOINTS_0"] = self.add_accessor(joints, GL_UNSIGNED_SHORT, len(used), "VEC4", target=GL_ARRAY_BUFFER)
            attributes["WEIGHTS_0"] = self.add_accessor(weights, GL_FLOAT, len(used), "VEC4", target=GL_ARRAY_BUFFER)
        return {
            "attributes": attributes,
            "indices": self.add_accessor(
                indices,
                GL_UNSIGNED_INT,
                len(indices),
                "SCALAR",
                target=GL_ELEMENT_ARRAY_BUFFER,
            ),
            "material": material_index,
            "mode": 4,
        }

    def add_accessor(
        self,
        values: array,
        component_type: int,
        count: int,
        value_type: str,
        *,
        target: int | None = None,
        minimum: list[float] | None = None,
        maximum: list[float] | None = None,
    ) -> int:
        view = self.add_buffer_view(array_bytes(values), target=target)
        accessor: dict = {
            "bufferView": view,
            "componentType": component_type,
            "count": count,
            "type": value_type,
        }
        if minimum is not None:
            accessor["min"] = minimum
        if maximum is not None:
            accessor["max"] = maximum
        index = len(self.document["accessors"])
        self.document["accessors"].append(accessor)
        return index

    def add_buffer_view(self, data: bytes, *, target: int | None = None) -> int:
        while len(self.binary) % 4:
            self.binary.append(0)
        offset = len(self.binary)
        self.binary.extend(data)
        view: dict = {"buffer": 0, "byteOffset": offset, "byteLength": len(data)}
        if target is not None:
            view["target"] = target
        index = len(self.document["bufferViews"])
        self.document["bufferViews"].append(view)
        return index

    def write(self, out_path: Path) -> dict:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        document = dict(self.document)
        for key in ("skins", "meshes", "materials", "textures", "images", "accessors", "bufferViews", "animations"):
            if not document.get(key):
                document.pop(key, None)
        if self.binary:
            while len(self.binary) % 4:
                self.binary.append(0)
            document["buffers"] = [{"byteLength": len(self.binary)}]

        json_bytes = b""
        total_size = 0
        for _ in range(3):
            json_bytes = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            json_bytes += b" " * ((-len(json_bytes)) % 4)
            total_size = 12 + 8 + len(json_bytes)
            if self.binary:
                total_size += 8 + len(self.binary)
            extras = document.get("extras")
            taildata = None
            if isinstance(extras, dict):
                taildata = extras.get("sunflowyrTaildata")
            if not isinstance(taildata, dict) or taildata.get("export_size") == total_size:
                break
            taildata["export_size"] = total_size
        with out_path.open("wb") as handle:
            handle.write(struct.pack("<4sII", b"glTF", 2, total_size))
            handle.write(struct.pack("<I4s", len(json_bytes), b"JSON"))
            handle.write(json_bytes)
            if self.binary:
                handle.write(struct.pack("<I4s", len(self.binary), b"BIN\0"))
                handle.write(self.binary)
        return self.metrics()

    def metrics(self) -> dict:
        return {
            "nodes": len(self.document["nodes"]),
            "skins": len(self.document["skins"]),
            "meshes": len(self.document["meshes"]),
            "materials": len(self.document["materials"]),
            "textures": len(self.document["textures"]),
            "animations": len(self.document.get("animations", [])),
            "format": "glb_2",
        }

def select_material_textures(material: SceneMaterial) -> tuple[SceneTexture | None, SceneTexture | None]:
    main_names = {"_maintex", "texture", "_texture"}
    normal_names = {"_normal", "normal"}
    main = None
    normal = None
    fallback = None
    for property_name, texture in material.textures.items():
        lowered = property_name.lower()
        if lowered in main_names and main is None:
            main = texture
        elif lowered in normal_names and normal is None:
            normal = texture
        elif fallback is None:
            fallback = texture
    return main or fallback, normal

def key_extras(key: ObjectKey, kind: str) -> dict:
    return {"unity_kind": kind, "unity_container": key[0], "unity_path_id": key[1]}

def array_bytes(values: array) -> bytes:
    if sys.byteorder == "little":
        return values.tobytes()
    copy = array(values.typecode, values)
    copy.byteswap()
    return copy.tobytes()

def vector_min_max(values: array, dimensions: int) -> tuple[list[float], list[float]]:
    minimum = [math.inf] * dimensions
    maximum = [-math.inf] * dimensions
    for offset in range(0, len(values), dimensions):
        for dimension in range(dimensions):
            value = float(values[offset + dimension])
            minimum[dimension] = min(minimum[dimension], value)
            maximum[dimension] = max(maximum[dimension], value)
    return minimum, maximum

def transform_point(matrix: Matrix, vector: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z = vector
    return (
        matrix[0][0] * x + matrix[0][1] * y + matrix[0][2] * z + matrix[0][3],
        matrix[1][0] * x + matrix[1][1] * y + matrix[1][2] * z + matrix[1][3],
        matrix[2][0] * x + matrix[2][1] * y + matrix[2][2] * z + matrix[2][3],
    )

def transform_vector(matrix: Matrix, vector: Iterable[float]) -> tuple[float, float, float]:
    x, y, z = vector
    return (
        matrix[0][0] * x + matrix[0][1] * y + matrix[0][2] * z,
        matrix[1][0] * x + matrix[1][1] * y + matrix[1][2] * z,
        matrix[2][0] * x + matrix[2][1] * y + matrix[2][2] * z,
    )

def uv_extra_names(index: int) -> tuple[str, str]:
    return (f"_UNITY_UV{index}_Z", f"_UNITY_UV{index}_W")

def vector_length(vector: Iterable[float]) -> float:
    return math.sqrt(sum(float(value) * float(value) for value in vector))

def normalize3(vector: Iterable[float]) -> tuple[float, float, float]:
    x, y, z = vector
    length = math.sqrt(x * x + y * y + z * z)
    if length < 1e-12:
        return 0.0, 0.0, 0.0
    return x / length, y / length, z / length

def near_tuple(left: tuple[float, ...], right: tuple[float, ...]) -> bool:
    return all(abs(a - b) < 1e-7 for a, b in zip(left, right))
