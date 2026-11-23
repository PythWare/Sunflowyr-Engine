from __future__ import annotations
import os
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

WORKER_MEMORY_MB = 520
MEMORY_FRACTION = 0.6
MAX_USEFUL_WORKERS = 6
BATCH_WEIGHT = 200
QUEUE_DEPTH = 4

JOB_WEIGHTS = {
    "raw_object": 2,
    "monobehaviour_json": 3,
    "editable_asset_json": 3,
    "map_component_json": 4,
    "audio": 80,
    "mesh_glb": 150,
    "png": 200,
    "animation_package": 200,
    "editable_asset_table": 200,
    "reconstructed_batch": 100000,
}
DEFAULT_JOB_WEIGHT = 5

@dataclass(slots=True)
class ExportJob:
    kind: str
    container: str
    path_id: int
    out_path: str
    extra: dict[str, Any] = field(default_factory=dict)

@dataclass(slots=True)
class JobResult:
    index: int
    status: str
    metadata: dict[str, Any] = field(default_factory=dict)
    export_size: int = 0
    counters: dict[str, int] = field(default_factory=dict)
    error: str = ""
    seconds: float = 0.0
    payload: Any = None

@dataclass(slots=True)
class PendingJob:
    kind: str
    out_path: Path
    metadata: dict[str, Any]
    success_stats: tuple[str, ...]
    unsupported_stat: str
    source: str = ""

context: dict[str, Any] = {}

def available_memory_mb() -> float | None:
    try:
        if os.name == "nt":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                fields = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32), ("total_physical", ctypes.c_uint64), ("free_physical", ctypes.c_uint64), ("total_page_file", ctypes.c_uint64), ("free_page_file", ctypes.c_uint64), ("total_virtual", ctypes.c_uint64), ("free_virtual", ctypes.c_uint64), ("free_extended", ctypes.c_uint64)]

            MemoryStatus._fields_ = MemoryStatus.fields
            status = MemoryStatus()
            status.length = ctypes.sizeof(MemoryStatus)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
            return status.free_physical / 1048576
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1048576
    except (OSError, ValueError, AttributeError):
        return None

def resolve_worker_count(requested: int | None) -> int:
    cores = os.cpu_count() or 1
    if requested is not None and requested > 0:
        return max(1, min(requested, 64))
    if requested is not None and requested < 0:
        return 1
    workers = max(1, cores - 1) if cores > 2 else 1
    workers = min(workers, MAX_USEFUL_WORKERS)
    memory = available_memory_mb()
    if memory is not None:
        affordable = int((memory * MEMORY_FRACTION) // WORKER_MEMORY_MB)
        workers = max(1, min(workers, affordable))
    return workers

def describe_worker_choice(workers: int) -> str:
    cores = os.cpu_count() or 1
    memory = available_memory_mb()
    if memory is None:
        return f"{workers} worker(s), {cores} logical cores, free memory couldnt be read"
    return (
        f"{workers} worker(s), {cores} logical cores, {memory / 1024:.1f} GB free, "
        f"budget {WORKER_MEMORY_MB} MB each, capped at {MAX_USEFUL_WORKERS} "
        f"because the single-threaded parent is the ceiling"
    )

def init_worker(data_root: str, warm: bool = True) -> None:
    context.clear()
    context["data_root"] = Path(data_root)
    if warm:
        load_assets()
        load_model_context()
        load_mono_scripts()
        load_managed_registry()

def load_assets() -> dict[str, Any]:
    cached = context.get("assets")
    if cached is not None:
        return cached
    from .serialized import discover_serialized_files, load_serialized_asset

    root = context["data_root"]
    assets: dict[str, Any] = {}
    objects: dict[str, dict[int, Any]] = {}
    for path in discover_serialized_files(root):
        asset = load_serialized_asset(path, root)
        key = asset.relative_path.replace("\\", "/").lower()
        assets[key] = asset
        objects[key] = {obj.path_id: obj for obj in asset.objects}
    context["assets"] = assets
    context["objects"] = objects
    return assets

def find_asset(container: str):
    return load_assets()[container.replace("\\", "/").lower()]

def find_object(container: str, path_id: int):
    load_assets()
    return context["objects"][container.replace("\\", "/").lower()][path_id]

def load_model_context():
    cached = context.get("model_context")
    if cached is None:
        from .model_context import build_model_context

        cached = build_model_context(list(load_assets().values()))
        context["model_context"] = cached
    return cached

def load_mono_scripts():
    cached = context.get("mono_scripts")
    if cached is None:
        from .mono_export import build_monoscript_index

        cached = build_monoscript_index(list(load_assets().values()))
        context["mono_scripts"] = cached
    return cached

def load_managed_registry():
    if "managed_registry" not in context:
        registry = None
        try:
            from .managed_schema import shared_registry

            registry = shared_registry(context["data_root"] / "Managed")
        except Exception:
            registry = None
        context["managed_registry"] = registry
    return context["managed_registry"]

def open_reader(container: str):
    from .binary import EndianReader

    handles = context.setdefault("handles", {})
    key = container.replace("\\", "/").lower()
    reader = handles.get(key)
    if reader is None:
        asset = find_asset(container)
        reader = EndianReader(asset.path.open("rb"), asset.endian)
        handles[key] = reader
    return reader

def describe(container: str, path_id: int):
    from .objects import describe_object

    return describe_object(find_asset(container), open_reader(container), find_object(container, path_id))

def run_raw_object(job: ExportJob, metadata: dict[str, Any]) -> tuple[int, dict[str, int]]:
    from .exporter import write_export_data
    from .raw_object_export import CompleteRawObject, build_complete_raw_object

    asset = find_asset(job.container)
    obj = find_object(job.container, job.path_id)
    desc = describe(job.container, job.path_id)
    reader = open_reader(job.container)
    reader.seek(obj.byte_start)
    original_data = reader.read(obj.byte_size)
    counters: dict[str, int] = {}
    payload_error = desc.details.get("payload_error")
    if payload_error and obj.class_id in {28, 83, 89}:
        complete_raw = CompleteRawObject(original_data, 0, None)
        metadata["raw_inlining_error"] = str(payload_error)
        counters["raw_inlining_errors"] = 1
    else:
        try:
            complete_raw = build_complete_raw_object(asset, obj, desc.payloads, original_data)
        except Exception as exc:
            complete_raw = CompleteRawObject(original_data, 0, None)
            metadata["raw_inlining_error"] = f"{type(exc).__name__}: {exc}"
            counters["raw_inlining_errors"] = 1
    write_export_data(complete_raw.data, Path(job.out_path))
    metadata["export_size"] = len(complete_raw.data)
    metadata["raw_self_contained"] = "raw_inlining_error" not in metadata
    counters["raw_object_bytes"] = len(complete_raw.data)
    if complete_raw.resource_kind:
        metadata["inlined_resource_kind"] = complete_raw.resource_kind
        metadata["inlined_resource_bytes"] = complete_raw.inlined_resource_bytes
        counters["raw_inlined_resource_objects"] = 1
        counters["raw_inlined_resource_bytes"] = complete_raw.inlined_resource_bytes
    return len(complete_raw.data), counters

def run_reconstructed(job: ExportJob, metadata: dict[str, Any]):
    from .exporter import reserve_output_path
    from .reconstructed_export import export_reconstructed_glbs

    extra = job.extra
    reserved: set[str] = set()
    batch = export_reconstructed_glbs(
        load_model_context(),
        Path(extra["output_root"]),
        embed_taildata=False,
        reserve_path=lambda path, source: reserve_output_path(path, reserved, source),
        kinds=set(extra["kinds"]) if extra.get("kinds") else None,
        files=set(extra["files"]) if extra.get("files") else None,
        limit=extra.get("limit"),
    )
    return 0, {}, batch

def run_png(job: ExportJob, metadata: dict[str, Any]) -> int:
    from .binary import read_range
    from .texture_convert import save_png

    extra = job.extra
    payload = b"".join(read_range(Path(extra["resource"]), extra["offset"], extra["size"]))
    out_path = Path(job.out_path)
    metadata["conversion"] = save_png(
        payload,
        int(extra["width"]),
        int(extra["height"]),
        int(extra["texture_format"]),
        out_path,
        flip_vertical=bool(extra["flip_vertical"]),
        image_count=int(extra["image_count"]),
        complete_image_size=int(extra["complete_image_size"]),
    )
    return out_path.stat().st_size

def run_audio(job: ExportJob, metadata: dict[str, Any]) -> int:
    from .audio_convert import save_audio_view
    from .binary import read_range

    extra = job.extra
    payload = b"".join(read_range(Path(extra["resource"]), extra["offset"], extra["size"]))
    out_path = Path(job.out_path)
    conversion = save_audio_view(payload, extra["details"], out_path)
    if conversion.get("format") == "ogg_vorbis_from_fsb5":
        metadata["kind"] = "audio_ogg"
        metadata["audio_writeback_supported"] = True
        metadata["audio_writeback_format"] = "audio_ogg_to_fsb5_vorbis"
        metadata["writeback_note"] = (
            "Replace this clip with PCM WAV while keeping the exported basename/path suffix, "
            "the AssetBundle generator will build a compatible inline FSB5 PCM AudioClip. OGG is also accepted "
            "when its setup packet matches a RWBY FMOD Vorbis profile."
        )
    else:
        metadata["audio_writeback_supported"] = True
        metadata["audio_writeback_format"] = "audio_wav_to_fsb5_pcm"
        metadata["writeback_note"] = (
            "Replace this clip with PCM WAV while keeping the exported basename/path suffix, "
            "the AssetBundle generator will build a compatible inline FSB5 PCM AudioClip."
        )
    metadata["conversion"] = conversion
    return out_path.stat().st_size

def simple(exporter: str):
    def run(job: ExportJob, metadata: dict[str, Any]) -> int:
        asset = find_asset(job.container)
        obj = find_object(job.container, job.path_id)
        out_path = Path(job.out_path)
        if exporter == "mesh_glb":
            from .mesh_glb_export import export_mesh_glb

            metadata["conversion"] = export_mesh_glb(asset, obj, out_path, load_model_context())
            metadata["writeback_note"] = (
                "Round-trip GLB can replace this existing Unity Mesh. Existing renderer material slots "
                "and skeleton object references are preserved."
            )
        elif exporter == "monobehaviour_json":
            from .mono_export import export_monobehaviour_json

            conversion = export_monobehaviour_json(
                asset, obj, out_path, metadata, load_mono_scripts(), load_managed_registry()
            )
            metadata["conversion"] = conversion
            if conversion.get("format") == "monobehaviour_json_typed_managed":
                return out_path.stat().st_size, {"mono_json_typed_exports": 1}
            return out_path.stat().st_size, {"mono_json_raw_fallbacks": 1}
        elif exporter == "monoscript_json":
            from .mono_export import export_monoscript_json

            metadata["conversion"] = export_monoscript_json(asset, obj, out_path, metadata)
        elif exporter == "animation_clip_json":
            from .animation_export import export_animation_clip_json

            metadata["conversion"] = export_animation_clip_json(asset, obj, out_path, metadata)
        elif exporter == "animation_package":
            from .animation_package import export_animation_package

            metadata["conversion"] = export_animation_package(
                asset, obj, load_model_context(), out_path, metadata
            )
        elif exporter == "animation_glb":
            from .animation_glb_export import export_animation_clip_glb

            metadata["conversion"] = export_animation_clip_glb(
                asset, obj, load_model_context(), out_path, metadata
            )
        elif exporter == "material_json":
            from .material_export import export_material_json

            metadata["conversion"] = export_material_json(asset, obj, out_path, metadata)
        elif exporter == "shader_package":
            from .shader_package import export_shader_package

            metadata["conversion"] = export_shader_package(asset, obj, out_path, metadata)
        elif exporter == "cgprogram_json":
            from .cgprogram_export import export_cgprogram_json

            metadata["conversion"] = export_cgprogram_json(asset, obj, out_path, metadata)
        elif exporter == "textmesh_json":
            from .textmesh_export import export_textmesh_json

            metadata["conversion"] = export_textmesh_json(
                asset, obj, out_path, metadata, job.extra.get("game_object_name", "")
            )
        elif exporter == "ui_json":
            from .ui_export import export_ui_component_json

            metadata["conversion"] = export_ui_component_json(
                asset, obj, out_path, metadata, job.extra.get("game_object_name", "")
            )
        elif exporter == "map_component_json":
            from .map_component_export import export_map_component_json

            metadata["conversion"] = export_map_component_json(
                asset, obj, load_model_context(), out_path, metadata
            )
        elif exporter == "editable_asset_json":
            from .editable_object_export import export_editable_asset_json

            metadata["conversion"] = export_editable_asset_json(
                asset, obj, load_model_context(), out_path, metadata, job.extra.get("asset_name", "")
            )
        elif exporter == "editable_asset_table":
            from .editable_table import export_editable_asset_table

            metadata["conversion"] = export_editable_asset_table(
                asset, obj, load_model_context(), out_path, metadata, job.extra.get("asset_name", "")
            )
        else:
            raise ValueError(f"unknown exporter {exporter}")
        return out_path.stat().st_size

    return run

def unsupported_errors(kind: str) -> tuple[type[BaseException], ...]:
    from .animation_export import AnimationClipExportError
    from .animation_glb_export import AnimationGlbExportError
    from .animation_package import AnimationPackageError
    from .audio_convert import AudioDecodeError
    from .cgprogram_export import CgProgramExportError
    from .editable_object_export import EditableObjectExportError
    from .editable_table import EditableTableError
    from .map_component_export import MapComponentExportError
    from .material_export import MaterialExportError
    from .mesh_convert import MeshDecodeError
    from .mono_export import MonoExportError
    from .shader_package import ShaderPackageError
    from .textmesh_export import TextMeshExportError
    from .texture_convert import TextureDecodeError
    from .ui_export import UiExportError

    table: dict[str, tuple[type[BaseException], ...]] = {
        "mesh_glb": (MeshDecodeError,),
        "monobehaviour_json": (MonoExportError,),
        "monoscript_json": (MonoExportError,),
        "animation_clip_json": (AnimationClipExportError,),
        "animation_package": (AnimationPackageError,),
        "animation_glb": (AnimationGlbExportError,),
        "material_json": (MaterialExportError,),
        "shader_package": (ShaderPackageError,),
        "cgprogram_json": (CgProgramExportError,),
        "textmesh_json": (TextMeshExportError,),
        "ui_json": (UiExportError,),
        "map_component_json": (MapComponentExportError,),
        "editable_asset_json": (EditableObjectExportError, EditableTableError),
        "editable_asset_table": (EditableObjectExportError, EditableTableError),
        "png": (TextureDecodeError,),
        "audio": (AudioDecodeError,),
    }
    return table.get(kind, ())

RUNNERS: dict[str, Callable[[ExportJob, dict[str, Any]], Any]] = {}

def runner_for(kind: str):
    if kind not in RUNNERS:
        if kind == "raw_object":
            RUNNERS[kind] = run_raw_object
        elif kind == "reconstructed_batch":
            RUNNERS[kind] = run_reconstructed
        elif kind == "png":
            RUNNERS[kind] = run_png
        elif kind == "audio":
            RUNNERS[kind] = run_audio
        else:
            RUNNERS[kind] = simple(kind)
    return RUNNERS[kind]

def execute_job(index: int, job: ExportJob) -> JobResult:
    import time

    metadata: dict[str, Any] = {}
    start = time.perf_counter()
    try:
        outcome = runner_for(job.kind)(job, metadata)
    except unsupported_errors(job.kind) as exc:
        return JobResult(
            index=index,
            status="unsupported",
            error=f"{type(exc).__name__}: {exc}",
            seconds=time.perf_counter() - start,
        )
    except Exception as exc:
        return JobResult(
            index=index,
            status="error",
            error=f"{type(exc).__name__}: {exc}",
            seconds=time.perf_counter() - start,
        )
    payload = None
    if isinstance(outcome, tuple) and len(outcome) == 3:
        size, counters, payload = outcome
    elif isinstance(outcome, tuple):
        size, counters = outcome
    else:
        size, counters = outcome, {}
    return JobResult(
        index=index,
        status="ok",
        metadata=metadata,
        export_size=int(size),
        counters=counters,
        seconds=time.perf_counter() - start,
        payload=payload,
    )

def execute_batch(batch: list[tuple[int, ExportJob]]) -> list[JobResult]:
    return [execute_job(index, job) for index, job in batch]

def seed_context(data_root: Path, assets: Iterable[Any], model_context: Any, mono_scripts: Any, managed_registry: Any) -> None:
    context.clear()
    context["data_root"] = Path(data_root)
    indexed: dict[str, Any] = {}
    objects: dict[str, dict[int, Any]] = {}
    for asset in assets:
        key = asset.relative_path.replace("\\", "/").lower()
        indexed[key] = asset
        objects[key] = {obj.path_id: obj for obj in asset.objects}
    context["assets"] = indexed
    context["objects"] = objects
    if model_context is not None:
        context["model_context"] = model_context
    if mono_scripts is not None:
        context["mono_scripts"] = mono_scripts
    context["managed_registry"] = managed_registry

class JobRunner:

    def __init__(self, data_root: Path, workers: int, on_complete: Callable[[PendingJob, JobResult], None]) -> None:
        self.workers = workers
        self.on_complete = on_complete
        self.pending: dict[int, PendingJob] = {}
        self.next_index = 0
        self.batch: list[tuple[int, ExportJob]] = []
        self.batch_weight = 0
        self.futures: list[Any] = []
        self.pool: ProcessPoolExecutor | None = None
        if workers > 1:
            self.pool = ProcessPoolExecutor(
                max_workers=workers, initializer=init_worker, initargs=(str(data_root),)
            )

    @property
    def parallel(self) -> bool:
        return self.pool is not None

    def submit(
        self,
        job: ExportJob,
        out_path: Path,
        metadata: dict[str, Any],
        success_stats: tuple[str, ...],
        unsupported_stat: str,
    ) -> None:

        index = self.next_index
        self.next_index += 1
        self.pending[index] = PendingJob(
            job.kind, out_path, metadata, success_stats, unsupported_stat,
            f"{job.container}:{job.path_id}",
        )
        if self.pool is None:
            self.deliver(execute_job(index, job))
            return
        self.batch.append((index, job))
        self.batch_weight += JOB_WEIGHTS.get(job.kind, DEFAULT_JOB_WEIGHT)
        if self.batch_weight >= BATCH_WEIGHT:
            self.flush_batch()
            self.reap(block=len(self.futures) > self.workers * QUEUE_DEPTH)

    def flush_batch(self) -> None:
        if not self.batch:
            return
        assert self.pool is not None

        self.futures.append(self.pool.submit(execute_batch, self.batch))
        self.batch = []
        self.batch_weight = 0

    def reap(self, block: bool) -> None:
        if not self.futures:
            return

        done = [future for future in self.futures if future.done()]
        if not done and block:
            done, pending = wait(self.futures, return_when=FIRST_COMPLETED)
        if not done:
            return
        finished = set(id(future) for future in done)
        for future in done:
            for result in future.result():
                self.deliver(result)
        self.futures = [future for future in self.futures if id(future) not in finished]

    def deliver(self, result: JobResult) -> None:
        pending = self.pending.pop(result.index, None)
        if pending is None:
            return
        self.on_complete(pending, result)

    def finish(self) -> None:
        self.flush_batch()
        while self.futures:
            self.reap(block=True)
        if self.pool is not None:
            self.pool.shutdown(wait=True)
            self.pool = None
