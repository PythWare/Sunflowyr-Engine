from __future__ import annotations
import json, re, struct
from functools import lru_cache
from pathlib import Path
from .asset_bundle_material import BundlePointerRef
from .serialized import load_serialized_asset
from .shader_export import ShaderExportError, read_shader_object

SHADER_CLASS_ID = 48
SHADER_ASSET_SUFFIX = ".shader.bin"
SHADER_DEPS_SUFFIX = ".shader.deps.json"
PPTR_SIZE = 12

class NewShaderAssetError(ValueError):
    pass

DECLARED_NAME_PATTERN = re.compile(r'Shader\s+"([^"]+)"')

def declared_shader_name(script: object) -> str:
    if isinstance(script, (bytes, bytearray)):
        script = script.decode("utf-8", "replace")
    match = DECLARED_NAME_PATTERN.search(str(script or ""))
    return match.group(1) if match else ""

@lru_cache(maxsize=4)
def build_shader_name_index(data_root: Path) -> dict[str, str]:
    names: dict[str, str] = {}
    data_root = Path(data_root)
    for candidate in sorted(data_root.iterdir()):
        if not candidate.is_file() or candidate.suffix not in (".assets", ""):
            continue
        try:
            asset = load_serialized_asset(candidate, data_root)
        except Exception:
            continue
        for obj in asset.objects:
            if obj.class_id != SHADER_CLASS_ID:
                continue
            try:
                shader = read_shader_object(asset, obj)
            except ShaderExportError:
                continue
            key = shader.name.strip().casefold()
            declared = declared_shader_name(shader.script)
            if key and declared:
                names.setdefault(key, declared)
    return names

@lru_cache(maxsize=4)
def safe_shader_file(name: str) -> str:
    return "".join(c if c.isalnum() or c in " -_." else "_" for c in name)

def shader_object_at(data_root: Path, container: str, path_id: int):
    candidate = Path(data_root) / container
    if not candidate.is_file():
        return None
    try:
        asset = load_serialized_asset(candidate, Path(data_root))
    except Exception:
        return None
    obj = next(
        (o for o in asset.objects if o.path_id == path_id and o.class_id == SHADER_CLASS_ID),
        None,
    )
    if obj is None:
        return None
    try:
        return asset, obj, read_shader_object(asset, obj)
    except ShaderExportError:
        return None

def export_shader_closure(
    data_root: Path,
    container: str,
    path_id: int,
    out_dir: Path,
    written: dict[tuple[str, int], str] | None = None,
) -> str | None:
    written = {} if written is None else written
    key = (container.replace("\\", "/").casefold(), int(path_id))
    if key in written:
        return written[key]

    found = shader_object_at(data_root, container, path_id)
    if found is None:
        return None
    asset, obj, shader = found
    raw = asset.path.read_bytes()[obj.byte_start : obj.byte_start + obj.byte_size]
    trailing = shader.trailing
    if not trailing or not raw.endswith(trailing):
        return None

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_shader_file(shader.name) or safe_shader_file(
        declared_shader_name(shader.script).replace("/", "_")
    ) or f"Shader_{path_id}"
    file_name = f"{stem}{SHADER_ASSET_SUFFIX}"
    written[key] = file_name
    (out_dir / file_name).write_bytes(raw)

    base = len(raw) - len(trailing)
    count = struct.unpack_from("<i", trailing, 0)[0]
    offsets: list[int] = []
    dependencies: list[str] = []
    for index in range(max(0, count)):
        file_index, dep_path_id = struct.unpack_from("<iq", trailing, 4 + index * PPTR_SIZE)
        if file_index == 0:
            dep_container = asset.relative_path.replace("\\", "/")
        elif 0 < file_index <= len(asset.externals):
            dep_container = str(asset.externals[file_index - 1]).replace("\\", "/")
        else:
            continue
        dep_file = export_shader_closure(
            data_root, dep_container, dep_path_id, out_dir, written
        )
        if dep_file is None:
            continue
        offsets.append(base + 4 + index * PPTR_SIZE)
        dependencies.append(dep_file)

    (out_dir / f"{stem}{SHADER_DEPS_SUFFIX}").write_text(
        json.dumps(
            {"pointerOffsets": offsets, "dependencies": dependencies},
            ensure_ascii=False,
            indent=2,
        )
        + chr(10),
        encoding="utf-8",
    )
    return file_name

def export_game_shader(data_root: Path, shader_name: str, out_dir: Path) -> str | None:
    wanted = shader_name.strip().casefold()
    if not wanted:
        return None
    location = build_shader_location_index(Path(data_root)).get(wanted)
    if location is None:
        return None
    container, path_id = location
    return export_shader_closure(data_root, container, path_id, out_dir)

@lru_cache(maxsize=4)
def build_shader_location_index(data_root: Path) -> dict[str, tuple[str, int]]:
    index: dict[str, tuple[str, int]] = {}
    data_root = Path(data_root)
    for candidate in sorted(data_root.iterdir()):
        if not candidate.is_file() or candidate.suffix not in (".assets", ""):
            continue
        try:
            asset = load_serialized_asset(candidate, data_root)
        except Exception:
            continue
        for obj in asset.objects:
            if obj.class_id != SHADER_CLASS_ID:
                continue
            try:
                shader = read_shader_object(asset, obj)
            except ShaderExportError:
                continue
            key = shader.name.strip().casefold()
            if key:
                index.setdefault(key, (asset.relative_path.replace("\\", "/"), obj.path_id))
    return index

def try_build_shader_asset(
    path: Path, fallback_name: str
) -> tuple[bytes, str, dict[str, object], tuple[BundlePointerRef, ...]] | None:
    path = Path(path)
    if not path.name.lower().endswith(SHADER_ASSET_SUFFIX):
        return None
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise NewShaderAssetError(f"couldnt read shader payload: {exc}") from exc
    if len(payload) < 8:
        raise NewShaderAssetError("shader payload is too small to be a Shader object")
    length = struct.unpack_from("<i", payload, 0)[0]
    if length < 0 or 4 + length > len(payload):
        raise NewShaderAssetError("shader payload doesnt start with a valid m_Name")
    try:
        name = payload[4 : 4 + length].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NewShaderAssetError(f"shader name isnt valid UTF-8: {exc}") from exc
    pointer_refs: list[BundlePointerRef] = []
    deps_path = path.with_name(path.name[: -len(SHADER_ASSET_SUFFIX)] + SHADER_DEPS_SUFFIX)
    dependencies: list[str] = []
    if deps_path.is_file():
        try:
            document = json.loads(deps_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise NewShaderAssetError(f"couldnt read shader dependencies: {exc}") from exc
        offsets = document.get("pointerOffsets") or []
        dependencies = document.get("dependencies") or []
        for offset, dependency in zip(offsets, dependencies):
            if not isinstance(offset, int) or not 0 <= offset <= len(payload) - PPTR_SIZE:
                raise NewShaderAssetError(
                    f"shader dependency offset {offset} is outside the payload"
                )
            pointer_refs.append(
                BundlePointerRef(
                    payload_offset=int(offset),
                    source_name=str(dependency),
                    slot="dependencies",
                )
            )
    details: dict[str, object] = {
        "sourceFormat": "game-shader-copy",
        "shaderName": name,
        "payloadBytes": len(payload),
        "fallbackShaders": dependencies,
    }
    return bytes(payload), name or fallback_name, details, tuple(pointer_refs)
